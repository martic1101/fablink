import asyncio

import pytest

from app.secs.codec import Item
from app.secs.hsms import ConnectionClosed, HsmsConnection, SecsMessage, T3Timeout


async def pair(server_handler, client_handler=None, t3=1.0, **client_kw):
    """Connect a passive (server) and active (client) HsmsConnection over loopback."""
    server_conn = {}
    ready = asyncio.Event()

    async def on_connect(reader, writer):
        server_conn["c"] = HsmsConnection(reader, writer, server_handler, t3=t3)
        server_conn["c"].start()
        ready.set()

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def noop(conn, msg):
        return None

    client = HsmsConnection(reader, writer, client_handler or noop, t3=t3, **client_kw)
    client.start()
    await ready.wait()
    return server, server_conn["c"], client


def test_header_pack_unpack():
    msg = SecsMessage(6, 11, Item.L(Item.U4(1)), w_bit=True, system=0xDEADBEEF, session_id=3)
    frame = msg.pack()
    assert int.from_bytes(frame[:4], "big") == len(frame) - 4
    out = SecsMessage.unpack(frame[4:])
    assert (out.stream, out.function, out.w_bit, out.system, out.session_id) == (6, 11, True, 0xDEADBEEF, 3)
    assert out.body == msg.body


async def test_select_request_reply():
    async def echo(conn, msg):
        return SecsMessage(msg.stream, msg.function + 1, msg.body)

    server, s, c = await pair(echo)
    await c.select()
    assert c.selected.is_set() and s.selected.is_set()
    reply = await c.request(1, 1, Item.A("ping"))
    assert reply.name == "S1F2" and reply.body.value == "ping"
    await c.close()
    server.close()


async def test_t3_timeout_when_equipment_does_not_reply():
    async def silent(conn, msg):
        await asyncio.sleep(10)

    server, _, c = await pair(silent, t3=0.2)
    with pytest.raises(T3Timeout):
        await c.request(1, 1)
    await c.close()
    server.close()


async def test_handler_exception_sends_transaction_abort():
    async def broken(conn, msg):
        raise RuntimeError("boom")

    server, _, c = await pair(broken)
    with pytest.raises(ConnectionError, match="S2F0"):
        await c.request(2, 41, Item.L())
    await c.close()
    server.close()


async def test_peer_primary_with_same_system_bytes_is_not_taken_as_reply():
    """Regression: both sides allocate system bytes independently. A primary
    from the tool reusing our pending system bytes must not resolve our request."""
    got_primary = asyncio.Event()

    async def server_handler(conn, msg):
        # Send an unrelated primary that reuses the pending request's system bytes,
        # then the real reply a bit later.
        await conn._write(SecsMessage(6, 1, Item.L(), False, msg.system, 0))
        await asyncio.sleep(0.05)
        return SecsMessage(1, 4, Item.A("real"))

    async def client_handler(conn, msg):
        got_primary.set()

    server, _, c = await pair(server_handler, client_handler)
    reply = await c.request(1, 3)
    assert reply.name == "S1F4" and reply.body.value == "real"
    await asyncio.wait_for(got_primary.wait(), 1)
    await c.close()
    server.close()


async def test_pending_request_fails_fast_when_link_drops():
    async def hang_then_close(conn, msg):
        await conn.close()

    server, _, c = await pair(hang_then_close, t3=5)
    with pytest.raises((ConnectionClosed, T3Timeout, ConnectionError)):
        await asyncio.wait_for(c.request(1, 1), 2)
    server.close()


async def test_linktest():
    async def noop(conn, msg):
        return None

    server, s, c = await pair(noop)
    await c.linktest()  # would raise T3Timeout if not answered
    await c.close()
    server.close()
