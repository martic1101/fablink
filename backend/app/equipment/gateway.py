"""Host-side link to one tool (the "EAP" — Equipment Automation Program).

Responsibilities:
  * keep an HSMS session up: connect, Select, S1F13, discover status variables
    via S1F11, then hand every primary message to the host;
  * reconnect with exponential backoff when the tool reboots or the network
    drops (the tool spools events meanwhile, so nothing is lost);
  * offer ``command()`` to send S2F41 remote commands with a T3 timeout.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set, Tuple

from ..secs.hsms import ConnectionClosed, HsmsConnection, SecsMessage, T3Timeout
from ..secs.messages import (
    HCACK,
    ControlState,
    EquipmentState,
    parse_s1f12,
    parse_s1f14,
    parse_s2f42,
    s2f41_body,
    s6f23_body,
)

log = logging.getLogger(__name__)

MessageHandler = Callable[["EquipmentLink", SecsMessage], Awaitable[Optional[SecsMessage]]]
StatusHandler = Callable[["EquipmentLink"], Awaitable[None]]
LogTap = Callable[["EquipmentLink", str, SecsMessage], None]
ActivityFn = Callable[["EquipmentLink", str, str, Optional[dict]], None]


class EquipmentLink:
    def __init__(
        self,
        tool_id: str,
        host: str,
        port: int,
        on_message: MessageHandler,
        on_status: StatusHandler,
        tap: Optional[LogTap] = None,
        on_activity: Optional[ActivityFn] = None,
        *,
        t3: float = 5.0,
        linktest: float = 10.0,
        max_backoff: float = 10.0,
    ) -> None:
        self.tool_id = tool_id
        self.host = host
        self.port = port
        self._on_message = on_message
        self._on_status = on_status
        self._tap = tap
        self._on_activity = on_activity
        self.t3 = t3
        self.linktest = linktest
        self.max_backoff = max_backoff

        self.conn: Optional[HsmsConnection] = None
        self.connected = False
        # Last state the tool reported. Whether it is reachable is ``connected``;
        # handlers from a dead session may still update this after a drop.
        self.state = EquipmentState.OFFLINE
        # What state_history currently says (the host's E10 record).
        self.recorded_state = EquipmentState.OFFLINE
        # GEM control state as last reported; LOCAL means a person is at the tool.
        self.control_state = ControlState.REMOTE
        self.tool_type: Optional[str] = None
        self.mdln: Optional[str] = None
        self.softrev: Optional[str] = None
        self.sensors: List[Tuple[int, str, str]] = []  # (svid, name, units)
        self.latest: List[float] = []
        self.trace: Deque[Tuple[str, List[float]]] = deque(maxlen=240)

        # Process tracking (filled from collection events)
        self.lot_id: Optional[str] = None
        self.lot_wafers = 0
        self.slot = 0
        self.wafer_samples: List[float] = []
        # A command was accepted but the matching state change hasn't arrived yet.
        self.busy = False
        # Between MAINT_STARTED and MAINT_COMPLETED. Needed because a tool under
        # repair stays DOWN, so the state alone can't tell "broken" from "being fixed".
        self.maint_active = False
        # The open maintenance work order, if any. While set, the tool is held:
        # the dispatcher sends it no new lots.
        self.work_order: Optional[Dict[str, Any]] = None
        # Recently processed events, to drop resends after a reply timeout.
        self._seen: Deque[Tuple[Any, ...]] = deque(maxlen=500)
        self._seen_set: Set[Tuple[Any, ...]] = set()
        self.last_error: Optional[str] = None
        # Latest raw S6F1 frame, kept so the UI can show how a reading is decoded.
        self.last_frame: Optional[Tuple[str, SecsMessage]] = None
        # Host-side bookkeeping for the "events replayed after reconnect" count.
        self.counting_replay = False
        self.replay_count = 0
        self.first_trace_pending = False
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._stop_event: Optional[asyncio.Event] = None

    # ---- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._task = asyncio.ensure_future(self._run())

    async def stop(self, timeout: float = 5.0) -> None:
        """Orderly shutdown: end the session, let the run loop and in-flight
        message handlers finish their database work, and only cancel what is
        still running after ``timeout``. Cancelling a task in the middle of a
        query can leave a database connection that never finishes closing."""
        self._stopping = True
        if self._stop_event:
            self._stop_event.set()
        if self.conn:
            await self.conn.close()
        if self._task:
            done, _ = await asyncio.wait({self._task}, timeout=timeout)
            if not done:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        if self.conn:
            await self.conn.drain(timeout)

    def _act(self, kind: str, text: str, data: Optional[dict] = None) -> None:
        if self._on_activity:
            self._on_activity(self, kind, text, data)

    async def _pause(self, seconds: float) -> None:
        """Sleep, but wake up at once when stop() is called."""
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        backoff = 0.5
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.debug("%s connect failed: %s", self.tool_id, exc)
            if self.connected:
                # A session that was up has ended.
                self.connected = False
                self.busy = False
                self.maint_active = False
                self._act(
                    "link.down",
                    f"HSMS session closed ({self.last_error or 'peer closed the connection'}). "
                    "Tool shown OFFLINE; reconnecting.",
                )
                await self._on_status(self)
                backoff = 0.5
            else:
                self._act(
                    "link.retry",
                    f"Connect to {self.host}:{self.port} failed ({self.last_error}). "
                    f"Next attempt in {backoff:g} s.",
                    {"backoff": backoff},
                )
            if self._stopping:
                return
            await self._pause(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.t3
        )
        if self._stopping:  # stop() was called while we were connecting
            writer.close()
            return
        conn = HsmsConnection(
            reader,
            writer,
            self._handle,
            t3=self.t3,
            linktest_interval=self.linktest,
            tap=(lambda d, m: self._tap(self, d, m)) if self._tap else None,
        )
        self.conn = conn
        conn.start()
        try:
            await conn.select()
            # S1F13 Establish Communications — retry until the tool accepts.
            while True:
                accepted, self.mdln, self.softrev = parse_s1f14((await conn.request(1, 13)).body)
                if accepted:
                    break
                await asyncio.sleep(1)
            self.tool_type = self.mdln.split("-")[0]
            # S1F11 Status Variable Namelist — learn what the trace values mean.
            self.sensors = parse_s1f12((await conn.request(1, 11)).body)
            self.connected = True
            self.last_error = None
            log.info("%s online (%s %s)", self.tool_id, self.mdln, self.softrev)
            await self._on_status(self)
            # S6F23: now that we're ready, ask the tool for what it spooled
            # while we were away. It holds its reports until we do.
            spool_note = "S6F23 asked for spooled data"
            try:
                await conn.request(6, 23, s6f23_body())
            except ConnectionClosed:
                raise
            except ConnectionError:
                spool_note = "tool does not support spooling (S6F23 aborted)"
            self._act(
                "link.up",
                f"Select OK, S1F13 accepted ({self.mdln} {self.softrev}), "
                f"S1F11 listed {len(self.sensors)} status variables, {spool_note}. Tool ONLINE.",
                {"sensors": len(self.sensors)},
            )
            await conn.wait_closed()
        finally:
            await conn.close()

    async def _handle(self, conn: HsmsConnection, msg: SecsMessage) -> Optional[SecsMessage]:
        return await self._on_message(self, msg)

    # ---- commands ---------------------------------------------------------------------------
    async def command(self, rcmd: str, params: Optional[Dict[str, str]] = None) -> HCACK:
        if not (self.conn and self.connected):
            raise ConnectionError(f"{self.tool_id} is offline")
        try:
            reply = await self.conn.request(2, 41, s2f41_body(rcmd, params))
        except T3Timeout:
            self.last_error = f"T3 timeout on {rcmd}"
            raise
        return parse_s2f42(reply.body)

    # ---- helpers --------------------------------------------------------------------------
    def sensor_index(self, name: str) -> Optional[int]:
        for i, (_, n, _) in enumerate(self.sensors):
            if n == name:
                return i
        return None

    def seen_before(self, key: Tuple[Any, ...]) -> bool:
        """Record ``key``; True if it was already processed recently."""
        if key in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        return False

    def add_trace(self, stime: str, values: List[float]) -> None:
        self.latest = values
        self.trace.append((stime, values))
