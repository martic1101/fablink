"""A simulated process tool that speaks HSMS/SECS-II like real equipment.

Physics, simplified:
  * every processed wafer adds ``wear`` to a consumable part (RF match network
    on etchers, vacuum pump on CVD tools);
  * one "health" sensor drifts with wear (with accelerating degradation), the
    others are just noisy;
  * above ``QUALITY_WEAR`` wafers start getting scrapped, at ``wear >= 1`` the
    part fails: an alarm is raised, the running lot is aborted and the tool
    goes DOWN until a technician repairs it;
  * a PM (preventive maintenance) resets wear and takes far less time than a
    repair. Doing the PM *before* the failure is the whole point of the host's
    predictive maintenance module.

GEM behaviour, simplified:
  * reports (S5F1 alarms, S6F11 events) go through one ordered outbox. While
    the host is unreachable it acts as the spool; on a new session the tool
    only transmits the spool after the host asks for it with S6F23;
  * a report whose reply times out (T3) is retried on the same session. The
    host may already have processed it, so it must drop duplicates (DATAID);
  * maintenance is not a remote command. A technician switches the tool to
    ONLINE LOCAL, works on it, and the tool reports MAINT_STARTED/COMPLETED.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Set, Tuple

from app.secs.codec import Item
from app.secs.hsms import ConnectionClosed, HsmsConnection, SecsMessage, T3Timeout
from app.secs.messages import (
    CEID,
    HCACK,
    RCMD,
    RSDC_PURGE,
    ControlState,
    EquipmentState,
    ack_body,
    parse_s2f41,
    s1f12_body,
    s1f14_body,
    s2f42_body,
    s5f1_body,
    s6f1_body,
    s6f11_body,
)

from .clock import SimClock

log = logging.getLogger("simulator")

QUALITY_WEAR = 0.88  # above this, wafers may be scrapped
NUISANCE_ALID = 9001


@dataclass
class Sensor:
    svid: int
    name: str
    units: str
    nominal: float
    noise: float
    drift: float = 0.0  # value added at wear == 1.0 (health sensor only)


@dataclass
class ToolSpec:
    tool_id: str
    tool_type: str
    mdln: str
    port: int
    sensors: List[Sensor]
    fault_alid: int
    fault_text: str
    wear_per_wafer: float
    softrev: str = "2.4.1"


ETCH_SENSORS = [
    Sensor(1, "RF_FWD_POWER", "W", 1500.0, 6.0),
    Sensor(2, "RF_REFL_POWER", "W", 12.0, 1.0, drift=40.0),
    Sensor(3, "CHAMBER_PRESSURE", "mTorr", 30.0, 0.4),
    Sensor(4, "ESC_TEMP", "degC", 60.0, 0.3),
]
CVD_SENSORS = [
    Sensor(1, "HEATER_TEMP", "degC", 400.0, 0.8),
    Sensor(2, "FORELINE_PRESSURE", "Torr", 0.15, 0.01, drift=0.45),
    Sensor(3, "CHAMBER_PRESSURE", "Torr", 2.5, 0.02),
    Sensor(4, "SIH4_FLOW", "sccm", 200.0, 1.0),
]


def default_fleet(base_port: int = 5001) -> List[ToolSpec]:
    def etch(n: int, wear: float) -> ToolSpec:
        return ToolSpec(
            f"ETCH0{n}",
            "ETCH",
            "ETCH-9000",
            base_port + n - 1,
            ETCH_SENSORS,
            1001,
            "RF reflected power high - matching network failure",
            wear,
        )

    def cvd(n: int, wear: float) -> ToolSpec:
        return ToolSpec(
            f"CVD0{n}",
            "CVD",
            "CVD-5000",
            base_port + 1 + n,
            CVD_SENSORS,
            2001,
            "Foreline pressure high - vacuum pump failure",
            wear,
        )

    # Different wear rates so tools don't all fail at the same moment.
    return [etch(1, 0.011), etch(2, 0.007), cvd(1, 0.009), cvd(2, 0.006)]


@dataclass
class _Lot:
    lot_id: str
    recipe: str
    wafers: int
    ok: int = 0
    scrapped: int = 0


@dataclass
class SimTool:
    spec: ToolSpec
    speed: float = 1.0
    wafer_seconds: float = 3.0
    trace_period: float = 0.5
    pm_seconds: float = 12.0
    repair_seconds: float = 45.0
    seed: Optional[int] = None
    initial_wear: float = 0.0
    t3: float = 5.0
    retry_seconds: float = 1.0  # real seconds between resends after a T3 timeout
    clock: SimClock = field(default_factory=SimClock)  # shared by the fleet; can be paused

    state: str = EquipmentState.IDLE
    control_state: str = ControlState.REMOTE
    wear: float = 0.0
    conn: Optional[HsmsConnection] = None
    communicating: bool = False
    lot: Optional[_Lot] = None
    # Ordered outbox of S5/S6 reports; it is the spool while the host is away.
    _spool: Deque[Tuple[int, int, Item]] = field(default_factory=lambda: deque(maxlen=500))
    _released: bool = False  # host asked for spooled data (S6F23) on this session
    _wake: Optional[asyncio.Event] = None
    retries: int = 0  # reports resent after a reply timeout
    _job: Optional[asyncio.Task] = None
    _server: Optional[asyncio.AbstractServer] = None
    _dataid: int = 0
    _sample: int = 0
    _alarm_active: bool = False
    # Scenario controls (see simulator/control.py). None of this is visible to
    # the host: it has to infer wear from sensor data, like in a real fab.
    wear_multiplier: float = 1.0
    _blocked_until: float = 0.0
    lose_next_reply: bool = False  # pretend the host's next reply never arrived

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.wear = self.initial_wear
        self._tasks: List[asyncio.Task] = []
        self._writers: Set[asyncio.StreamWriter] = set()  # every accepted connection

    # ---- scenario controls -------------------------------------------------------------------
    def status(self) -> Dict[str, object]:
        loop = asyncio.get_event_loop()
        return {
            "id": self.spec.tool_id,
            "tool_type": self.spec.tool_type,
            "port": self.spec.port,
            "state": self.state,
            "control_state": self.control_state,
            "wear": round(self.wear, 4),
            "wear_multiplier": self.wear_multiplier,
            "connected": bool(self.conn and not self.conn.closed.is_set()),
            "communicating": self.communicating,
            "spooled": len(self._spool),
            "retries": self.retries,
            "blocked_for": round(max(0.0, self._blocked_until - loop.time()), 1),
            "lot": self.lot.lot_id if self.lot else None,
        }

    async def disconnect(self, seconds: float) -> None:
        """Simulate a pulled network cable: drop the session and refuse new
        connections for ``seconds`` (real time, not scaled by ``speed``)."""
        self._blocked_until = asyncio.get_event_loop().time() + seconds
        if self.conn:
            await self.conn.close()

    # ---- time helpers --------------------------------------------------------------------
    async def _sleep(self, seconds: float) -> None:
        """Simulated seconds: scaled by ``speed`` and frozen while paused."""
        await self.clock.sleep(seconds / self.speed)

    # ---- sensors -----------------------------------------------------------------------------
    def read_sensors(self) -> List[float]:
        processing = self.state == EquipmentState.PROCESSING
        values = []
        for s in self.spec.sensors:
            if not processing and s.drift == 0.0 and s.nominal > 100:
                base = s.nominal * 0.02  # RF off / heater at idle setpoint
            else:
                base = s.nominal
            # accelerating degradation: most drift happens late in life
            drift = s.drift * (0.35 * self.wear + 0.65 * self.wear**3)
            values.append(round(base + drift + self.rng.gauss(0, s.noise), 4))
        return values

    # ---- HSMS server -------------------------------------------------------------------------
    async def serve(self, host: str = "127.0.0.1") -> None:
        self._server = await asyncio.start_server(self._on_connect, host, self.spec.port)
        self.spec.port = self._server.sockets[0].getsockname()[1]  # resolves port 0 in tests
        self._wake = asyncio.Event()
        self._tasks = [asyncio.ensure_future(self._trace_loop()), asyncio.ensure_future(self._sender())]
        log.info("%s listening on %s:%d", self.spec.tool_id, host, self.spec.port)

    async def stop(self) -> None:
        for task in (self._job, *self._tasks):
            if task:
                task.cancel()
        if self.conn:
            await self.conn.close()
        if self._server:
            self._server.close()
            # Since Python 3.12.1, wait_closed() also waits for every accepted
            # connection to close, so close them all ourselves (3.9 returned at once).
            for writer in list(self._writers):
                writer.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except asyncio.TimeoutError:
                log.warning("%s: connections still open after stop", self.spec.tool_id)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            await self._serve_connection(reader, writer)
        finally:
            self._writers.discard(writer)

    async def _serve_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if asyncio.get_event_loop().time() < self._blocked_until:
            writer.close()  # "cable pulled"
            return
        if self.conn and not self.conn.closed.is_set():
            # HSMS-SS: only one host session; refuse the newcomer.
            writer.close()
            return
        conn = HsmsConnection(reader, writer, self._handle, t3=self.t3)
        self.conn = conn
        self.communicating = self._released = False
        conn.start()
        log.info("%s: host connected", self.spec.tool_id)
        await conn.wait_closed()
        self.communicating = self._released = False
        log.info("%s: host disconnected", self.spec.tool_id)

    async def _handle(self, conn: HsmsConnection, msg: SecsMessage) -> Optional[SecsMessage]:
        sf = (msg.stream, msg.function)
        if sf == (1, 1):
            return SecsMessage(1, 2, Item.L(Item.A(self.spec.mdln), Item.A(self.spec.softrev)))
        if sf == (1, 13):
            self.communicating = True
            return SecsMessage(1, 14, s1f14_body(self.spec.mdln, self.spec.softrev))
        if sf == (1, 3):
            vals = dict(zip((s.svid for s in self.spec.sensors), self.read_sensors()))
            wanted = [i.scalar for i in msg.body.value] if msg.body else []
            return SecsMessage(1, 4, Item.L(*[Item.F4(vals.get(i, 0.0)) for i in wanted or vals]))
        if sf == (1, 11):
            return SecsMessage(1, 12, s1f12_body([(s.svid, s.name, s.units) for s in self.spec.sensors]))
        if sf == (2, 41):
            rcmd, params = parse_s2f41(msg.body)
            return SecsMessage(2, 42, s2f42_body(self.remote_command(rcmd, params)))
        if sf == (6, 23):
            return SecsMessage(6, 24, ack_body(self._request_spool(msg.body.scalar if msg.body else 0)))
        return SecsMessage(msg.stream, 0)  # abort: unsupported

    def _request_spool(self, rsdc: int) -> int:
        """S6F23: the host is ready for what we held while it was away."""
        if rsdc == RSDC_PURGE:
            self._spool.clear()
            return 0
        self._released = True
        # Marks the end of the replay for the host, like a GEM "online" event.
        self._event(CEID.STATE_CHANGED, [self.state, "communication established"])
        return 0

    # ---- remote commands -------------------------------------------------------------------
    @property
    def job_running(self) -> bool:
        return self._job is not None and not self._job.done()

    def remote_command(self, rcmd: str, params: Dict[str, str]) -> HCACK:
        if rcmd not in (RCMD.START, RCMD.ABORT):
            return HCACK.INVALID_COMMAND
        if self.control_state == ControlState.LOCAL:
            return HCACK.CANNOT_PERFORM_NOW  # someone is working at the tool
        if rcmd == RCMD.START:
            if self.state != EquipmentState.IDLE or self.job_running:
                return HCACK.CANNOT_PERFORM_NOW
            try:
                lot = _Lot(params["LOTID"], params["RECIPE"], int(params["WAFERS"]))
            except (KeyError, ValueError):
                return HCACK.PARAMETER_INVALID
            if lot.wafers <= 0 or lot.wafers > 25:
                return HCACK.PARAMETER_INVALID
            self._job = asyncio.ensure_future(self._run_lot(lot))
            return HCACK.ACCEPTED_WILL_FINISH_LATER
        if self.state != EquipmentState.PROCESSING or not self.job_running:
            return HCACK.CANNOT_PERFORM_NOW
        self._job.cancel()
        return HCACK.OK

    # ---- local maintenance (done by a technician at the tool) -----------------------------
    def can_maintain(self, kind: str) -> bool:
        if self.job_running:
            return False
        return self.state == (EquipmentState.IDLE if kind == "PM" else EquipmentState.DOWN)

    async def local_maintenance(self, kind: str, work_order: int) -> None:
        if not self.can_maintain(kind):
            raise RuntimeError(f"{self.spec.tool_id} cannot start {kind} in state {self.state}")
        self._job = asyncio.ensure_future(self._maintain(kind, work_order))
        await self._job

    async def _maintain(self, kind: str, work_order: int) -> None:
        self._set_control(ControlState.LOCAL)
        state = EquipmentState.PM if kind == "PM" else EquipmentState.DOWN
        self._set_state(state, f"{kind} in progress, work order {work_order}")
        self._event(CEID.MAINT_STARTED, [kind, work_order])
        await self._sleep(self.pm_seconds if kind == "PM" else self.repair_seconds)
        self.wear = 0.0
        self.wear_multiplier = 1.0  # the new part wears normally
        if self._alarm_active:
            self._alarm_active = False
            self._alarm(self.spec.fault_alid, self.spec.fault_text, False, severity=2)
        self._event(CEID.MAINT_COMPLETED, [kind, work_order])
        self._set_control(ControlState.REMOTE)
        self._set_state(EquipmentState.IDLE, f"{kind} complete")

    # ---- process behaviour -----------------------------------------------------------------
    def _set_state(self, state: str, reason: str) -> None:
        self.state = state
        self._event(CEID.STATE_CHANGED, [state, reason])

    def _set_control(self, control_state: str) -> None:
        self.control_state = control_state
        self._event(CEID.CONTROL_STATE_CHANGED, [control_state])

    async def _run_lot(self, lot: _Lot) -> None:
        self.lot = lot
        self._set_state(EquipmentState.PROCESSING, f"lot {lot.lot_id}")
        self._event(CEID.LOT_STARTED, [lot.lot_id, lot.recipe, lot.wafers])
        try:
            for slot in range(1, lot.wafers + 1):
                self._event(CEID.WAFER_STARTED, [lot.lot_id, slot])
                await self._sleep(self.wafer_seconds)
                self.wear += self.spec.wear_per_wafer * self.wear_multiplier * self.rng.uniform(0.7, 1.3)
                if self.wear >= 1.0:
                    lot.scrapped += 1
                    self._event(CEID.WAFER_COMPLETED, [lot.lot_id, slot, "SCRAP"])
                    self._fault(lot)
                    return
                scrap_p = 0.0 if self.wear < QUALITY_WEAR else (self.wear - QUALITY_WEAR) * 3
                result = "SCRAP" if self.rng.random() < scrap_p else "OK"
                if result == "OK":
                    lot.ok += 1
                else:
                    lot.scrapped += 1
                self._event(CEID.WAFER_COMPLETED, [lot.lot_id, slot, result])
                if self.rng.random() < 0.02:
                    asyncio.ensure_future(self._nuisance_alarm())
            self._event(CEID.LOT_COMPLETED, [lot.lot_id, lot.ok, lot.scrapped])
            self._set_state(EquipmentState.IDLE, "lot complete")
        except asyncio.CancelledError:
            self._event(CEID.LOT_ABORTED, [lot.lot_id, "aborted by host"])
            self._set_state(EquipmentState.IDLE, "lot aborted")
        finally:
            self.lot = None

    def _fault(self, lot: _Lot) -> None:
        self._alarm_active = True
        self._alarm(self.spec.fault_alid, self.spec.fault_text, True, severity=2)
        self._event(CEID.LOT_ABORTED, [lot.lot_id, self.spec.fault_text])
        self._set_state(EquipmentState.DOWN, self.spec.fault_text)

    async def _nuisance_alarm(self) -> None:
        self._alarm(NUISANCE_ALID, "Wafer transfer robot retry", True, severity=7)
        await self._sleep(2.0)
        self._alarm(NUISANCE_ALID, "Wafer transfer robot retry", False, severity=7)

    async def _trace_loop(self) -> None:
        """S6F1 trace data: fast while processing, slow while idle."""
        while True:
            fast = self.state == EquipmentState.PROCESSING
            await self._sleep(self.trace_period if fast else self.trace_period * 4)
            if not (self.conn and self.communicating):
                continue  # trace data is not spooled
            self._sample += 1
            stime = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            try:
                await self.conn.send(6, 1, s6f1_body(1, self._sample, stime, self.read_sensors()))
            except ConnectionError:
                pass

    # ---- reporting: one ordered outbox -----------------------------------------------------
    def _event(self, ceid: CEID, values: List[object]) -> None:
        self._dataid += 1
        self._report(6, 11, s6f11_body(self._dataid, ceid, values))

    def _alarm(self, alid: int, text: str, is_set: bool, severity: int) -> None:
        self._report(5, 1, s5f1_body(alid, text, is_set, severity))

    def _report(self, stream: int, function: int, body: Item) -> None:
        self._spool.append((stream, function, body))
        if self._wake:
            self._wake.set()

    async def _sender(self) -> None:
        """Deliver reports in order, one at a time, each with a W-bit.

        * Nothing goes out until the host has asked for spooled data (S6F23).
        * If the session drops, the report goes back to the head of the spool.
        * If the reply times out (T3) the session may still be fine and the
          host may even have processed it, so resend it after a pause rather
          than stalling the whole outbox until the next reconnect. Delivery is
          at-least-once; the host drops duplicates by DATAID.
        """
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._spool and self.conn and self.communicating and self._released:
                item = self._spool.popleft()
                try:
                    await self.conn.request(*item)
                    if self.lose_next_reply:
                        # Scenario control: act as if the reply was lost on the way.
                        self.lose_next_reply = False
                        raise T3Timeout("reply lost (scenario)")
                except T3Timeout:
                    self._spool.appendleft(item)
                    self.retries += 1
                    log.warning("%s: no reply to S%dF%d, resending", self.spec.tool_id, item[0], item[1])
                    await asyncio.sleep(self.retry_seconds)
                except ConnectionClosed:
                    self._spool.appendleft(item)
                    break
                except ConnectionError:
                    log.warning("%s: host aborted S%dF%d, dropping it", self.spec.tool_id, item[0], item[1])
