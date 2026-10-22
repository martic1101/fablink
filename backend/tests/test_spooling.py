"""GEM-style reporting from the tool side: ordered delivery, spooling until the
host asks with S6F23, and resending after a reply timeout."""

import asyncio

from app.secs.hsms import HsmsConnection, SecsMessage
from app.secs.messages import ack_body, parse_s6f11, s2f41_body, s6f23_body
from simulator.tool import SimTool, default_fleet


async def start_tool(**kw):
    spec = default_fleet(0)[0]
    spec.port = 0
    tool = SimTool(spec, speed=40, seed=1, **kw)
    await tool.serve()
    return tool


async def fake_host(tool, handler, spool_request=True):
    reader, writer = await asyncio.open_connection("127.0.0.1", tool.spec.port)
    conn = HsmsConnection(reader, writer, handler, t3=2)
    conn.start()
    await conn.select()
    await conn.request(1, 13)
    if spool_request:
        await conn.request(6, 23, s6f23_body())
    return conn


async def until(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond():
        assert asyncio.get_event_loop().time() < end, "condition not met in time"
        await asyncio.sleep(0.02)


async def test_reply_timeout_resends_and_does_not_stall():
    """Regression: one T3 timeout used to park every later report in the spool
    until the next reconnect, so the host stopped hearing from the tool."""
    tool = await start_tool(t3=0.3, retry_seconds=0.05)
    seen = []
    delayed = []

    async def handler(conn, msg):
        if (msg.stream, msg.function) == (6, 11):
            seen.append(parse_s6f11(msg.body).dataid)
            if not delayed:
                delayed.append(True)
                await asyncio.sleep(0.6)  # longer than the tool's T3
            return SecsMessage(6, 12, ack_body())
        if msg.w_bit:
            return SecsMessage(msg.stream, msg.function + 1, ack_body())
        return None

    conn = await fake_host(tool, handler)
    await conn.request(2, 41, s2f41_body("START", {"LOTID": "L1", "RECIPE": "R", "WAFERS": "3"}))
    await until(lambda: len(set(seen)) >= 8)  # marker, PROCESSING, LOT_STARTED, wafers…
    assert seen.count(seen[0]) == 2  # the timed-out report was sent again
    assert tool.retries >= 1
    assert not conn.closed.is_set()  # all on the same session
    await conn.close()
    await tool.stop()


async def test_spool_is_held_until_host_sends_s6f23():
    tool = await start_tool()
    tool._alarm(4242, "raised while host was away", True, severity=5)
    got = []

    async def handler(conn, msg):
        if msg.stream in (5, 6) and msg.function in (1, 11):
            got.append((msg.stream, msg.function))
        return SecsMessage(msg.stream, msg.function + 1, ack_body()) if msg.w_bit else None

    conn = await fake_host(tool, handler, spool_request=False)
    await asyncio.sleep(0.3)
    assert (5, 1) not in got and len(tool._spool) == 1  # not until the host asks
    await conn.request(6, 23, s6f23_body())
    await until(lambda: (5, 1) in got and (6, 11) in got)
    assert got.index((5, 1)) < got.index((6, 11))  # spooled alarm first, then the marker
    await conn.close()
    await tool.stop()
