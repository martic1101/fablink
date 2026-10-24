"""HSMS (SEMI E37) — SECS message transport over TCP/IP.

Each frame on the wire is::

    4-byte length | 10-byte header | SECS-II body (optional)

Header layout::

    0-1  session id (device id)
    2    W-bit (0x80) | stream
    3    function
    4    PType (0 = SECS-II)
    5    SType (0 = data, 1/2 select.req/rsp, 5/6 linktest.req/rsp, 9 separate)
    6-9  system bytes (transaction id, echoed by the reply)

``HsmsConnection`` is symmetric: the host (active, connects) and the
equipment (passive, listens) use the same class. It handles control messages,
pairs replies with requests via the system bytes and enforces the T3 reply
timeout so a hung tool can never block the host forever.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Awaitable, Callable, Dict, Optional, Set, Tuple

from .codec import Item, decode, encode

log = logging.getLogger(__name__)

HEADER_LEN = 10
MAX_FRAME = 16 * 1024 * 1024


class SType(IntEnum):
    DATA = 0
    SELECT_REQ = 1
    SELECT_RSP = 2
    DESELECT_REQ = 3
    DESELECT_RSP = 4
    LINKTEST_REQ = 5
    LINKTEST_RSP = 6
    REJECT_REQ = 7
    SEPARATE_REQ = 9


class T3Timeout(TimeoutError):
    """Reply not received within T3 (SEMI E37 reply timeout)."""


class ConnectionClosed(ConnectionError):
    pass


@dataclass
class SecsMessage:
    stream: int
    function: int
    body: Optional[Item] = None
    w_bit: bool = False
    system: int = 0
    session_id: int = 0
    stype: SType = SType.DATA

    @property
    def name(self) -> str:
        return f"S{self.stream}F{self.function}"

    def to_sml(self) -> str:
        head = f"{self.name}{' W' if self.w_bit else ''}"
        if self.body is None:
            return f"{head}\n."
        return f"{head}\n{self.body.to_sml(1)}\n."

    # ---- wire format ----------------------------------------------------------------
    def pack(self) -> bytes:
        body = encode(self.body) if self.body is not None else b""
        header = struct.pack(
            ">HBBBBI",
            self.session_id,
            (0x80 if self.w_bit else 0) | (self.stream & 0x7F),
            self.function,
            0,
            int(self.stype),
            self.system,
        )
        return struct.pack(">I", HEADER_LEN + len(body)) + header + body

    @classmethod
    def unpack(cls, frame: bytes) -> SecsMessage:
        if len(frame) < HEADER_LEN:
            raise ValueError("HSMS frame shorter than header")
        session_id, b2, b3, ptype, stype, system = struct.unpack(">HBBBBI", frame[:HEADER_LEN])
        if ptype != 0:
            raise ValueError(f"unsupported PType {ptype}")
        body = decode(frame[HEADER_LEN:]) if len(frame) > HEADER_LEN else None
        return cls(
            stream=b2 & 0x7F,
            function=b3,
            body=body,
            w_bit=bool(b2 & 0x80),
            system=system,
            session_id=session_id,
            stype=SType(stype),
        )


Handler = Callable[["HsmsConnection", SecsMessage], Awaitable[Optional[SecsMessage]]]
Tap = Callable[[str, SecsMessage], None]


class HsmsConnection:
    """One HSMS session over an established TCP stream.

    ``handler`` is called for every primary data message received; if it
    returns a message and the primary had the W-bit set, that message is sent
    as the reply. ``tap`` (optional) sees every data message in both
    directions — the host uses it to keep a SECS message log.
    """

    _system_counter = itertools.count(1)

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handler: Handler,
        *,
        session_id: int = 0,
        t3: float = 5.0,
        linktest_interval: Optional[float] = None,
        tap: Optional[Tap] = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handler = handler
        self.session_id = session_id
        self.t3 = t3
        self.linktest_interval = linktest_interval
        self._tap = tap
        # system bytes -> (future, expected reply stream, expected reply function)
        self._pending: Dict[int, Tuple[asyncio.Future, int, int]] = {}
        self._write_lock = asyncio.Lock()
        self.selected = asyncio.Event()
        self.closed = asyncio.Event()
        self._tasks: list = []
        # Handlers for primaries we received; they may still be writing to the
        # database after the link closes, so shutdown waits for them (drain()).
        self._handlers: Set[asyncio.Task] = set()

    # ---- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        self._tasks.append(asyncio.ensure_future(self._read_loop()))
        if self.linktest_interval:
            self._tasks.append(asyncio.ensure_future(self._linktest_loop()))

    async def wait_closed(self) -> None:
        await self.closed.wait()

    async def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for fut, _, _ in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionClosed("connection closed"))
        self._pending.clear()
        current = asyncio.current_task()
        for t in self._tasks:
            if t is not current:
                t.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def drain(self, timeout: float) -> None:
        """Wait for message handlers that are still running, then cancel any
        stragglers. Call after close() during an orderly shutdown."""
        if not self._handlers:
            return
        _, pending = await asyncio.wait(set(self._handlers), timeout=timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # ---- sending ----------------------------------------------------------------------
    @classmethod
    def next_system(cls) -> int:
        return next(cls._system_counter) & 0xFFFFFFFF

    async def _write(self, msg: SecsMessage) -> None:
        if self.closed.is_set():
            raise ConnectionClosed("connection closed")
        if msg.stype is SType.DATA and self._tap:
            self._tap("out", msg)
        async with self._write_lock:
            self._writer.write(msg.pack())
            await self._writer.drain()

    async def _request(self, msg: SecsMessage, timeout: float) -> SecsMessage:
        fut = asyncio.get_event_loop().create_future()
        reply_fn = msg.function + 1 if msg.stype is SType.DATA else 0
        self._pending[msg.system] = (fut, msg.stream, reply_fn)
        try:
            await self._write(msg)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise T3Timeout(f"no reply to {msg.name} within {timeout}s") from exc
        finally:
            self._pending.pop(msg.system, None)

    async def send(self, stream: int, function: int, body: Optional[Item] = None) -> None:
        """Send a primary message that expects no reply."""
        await self._write(SecsMessage(stream, function, body, False, self.next_system(), self.session_id))

    async def request(self, stream: int, function: int, body: Optional[Item] = None) -> SecsMessage:
        """Send a primary with W-bit and wait (up to T3) for the reply."""
        msg = SecsMessage(stream, function, body, True, self.next_system(), self.session_id)
        reply = await self._request(msg, self.t3)
        if reply.function == 0:
            raise ConnectionError(f"{msg.name} aborted by peer (S{reply.stream}F0)")
        return reply

    async def select(self) -> None:
        """Active side: perform HSMS Select.req."""
        msg = SecsMessage(0, 0, None, False, self.next_system(), 0xFFFF, SType.SELECT_REQ)
        rsp = await self._request(msg, self.t3)
        if rsp.function != 0:  # SelectStatus is carried in byte 3
            raise ConnectionError(f"select rejected, status={rsp.function}")
        self.selected.set()

    async def linktest(self) -> None:
        msg = SecsMessage(0, 0, None, False, self.next_system(), 0xFFFF, SType.LINKTEST_REQ)
        await self._request(msg, self.t3)

    # ---- receiving --------------------------------------------------------------------
    async def _read_frame(self) -> bytes:
        raw_len = await self._reader.readexactly(4)
        (length,) = struct.unpack(">I", raw_len)
        if length < HEADER_LEN or length > MAX_FRAME:
            raise ValueError(f"invalid HSMS frame length {length}")
        return await self._reader.readexactly(length)

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = SecsMessage.unpack(await self._read_frame())
                await self._dispatch(msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # malformed frame: HSMS says drop the connection
            log.exception("HSMS protocol error, closing connection")
        finally:
            await self.close()

    async def _dispatch(self, msg: SecsMessage) -> None:
        st = msg.stype
        if st is SType.SELECT_REQ:
            self.selected.set()
            await self._write(SecsMessage(0, 0, None, False, msg.system, 0xFFFF, SType.SELECT_RSP))
        elif st is SType.LINKTEST_REQ:
            await self._write(SecsMessage(0, 0, None, False, msg.system, 0xFFFF, SType.LINKTEST_RSP))
        elif st in (SType.SELECT_RSP, SType.LINKTEST_RSP, SType.DESELECT_RSP):
            self._resolve(msg)
        elif st is SType.SEPARATE_REQ:
            await self.close()
        elif st is SType.DATA:
            if self._tap:
                self._tap("in", msg)
            if not msg.w_bit and self._is_reply(msg):
                self._resolve(msg)
            else:
                # Handle primaries concurrently so a slow handler can't stall replies.
                task = asyncio.ensure_future(self._handle_primary(msg))
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)

    def _is_reply(self, msg: SecsMessage) -> bool:
        """A data message is a reply only if its system bytes match a pending
        request AND it is SxF(n+1) or the SxF0 abort for that stream. Without the
        stream/function check, a primary from the peer that happens to reuse the
        same system bytes would be mistaken for our reply."""
        entry = self._pending.get(msg.system)
        if entry is None:
            return False
        _, stream, function = entry
        return msg.stream == stream and msg.function in (function, 0)

    def _resolve(self, msg: SecsMessage) -> None:
        entry = self._pending.get(msg.system)
        if entry and not entry[0].done():
            entry[0].set_result(msg)

    async def _handle_primary(self, msg: SecsMessage) -> None:
        try:
            reply = await self._handler(self, msg)
        except Exception:
            log.exception("handler failed for %s", msg.name)
            reply = None
            if msg.w_bit:  # SxF0 = transaction abort
                reply = SecsMessage(msg.stream, 0)
        if reply is not None and msg.w_bit:
            reply.system = msg.system
            reply.session_id = msg.session_id
            reply.w_bit = False
            try:
                await self._write(reply)
            except ConnectionClosed:
                pass

    async def _linktest_loop(self) -> None:
        assert self.linktest_interval
        try:
            while not self.closed.is_set():
                await asyncio.sleep(self.linktest_interval)
                await self.linktest()
        except (T3Timeout, ConnectionError):
            log.warning("linktest failed, dropping connection")
            await self.close()
