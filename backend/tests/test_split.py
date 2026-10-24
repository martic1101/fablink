"""The cloud split: fab daemon <-Redis-> stateless API, with a shared database.

Runs the real daemon (simulator, technicians, host) and the real RemoteFab /
cloud API in one event loop, connected through an in-memory fake Redis.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import fakeredis
import httpx
import pytest
from sqlalchemy import func, select

from app.commands import CommandError
from app.config import Settings
from app.daemon import FabDaemon
from app.db import create_schema, make_engine, make_sessionmaker, normalize_database_url
from app.models import EventLog
from app.remote import RemoteFab
from app.remote_app import create_remote_app

pytestmark = pytest.mark.integration


async def until(cond, timeout=10.0):
    end = asyncio.get_event_loop().time() + timeout
    while True:
        value = await cond() if asyncio.iscoroutinefunction(cond) else cond()
        if value:
            return value
        assert asyncio.get_event_loop().time() < end, "condition not met in time"
        await asyncio.sleep(0.05)


@pytest.fixture
async def split(tmp_path):
    server = fakeredis.FakeServer()
    url = f"sqlite+aiosqlite:///{tmp_path}/split.db"
    engine = make_engine(url)
    await create_schema(engine)
    sm = make_sessionmaker(engine)
    settings = Settings(
        database_url=url,
        equipment=[],
        dispatch_interval=0.02,
        wip_target=2,
        t3_seconds=2,
        sim_speed=40,
        idle_pause_seconds=0.5,
        snapshot_every=0.1,
    )
    daemon = FabDaemon(settings, fakeredis.FakeAsyncRedis(server=server), sm, base_port=0)
    await daemon.start()
    remote = RemoteFab(fakeredis.FakeAsyncRedis(server=server), sm, snapshot_ttl=0)
    yield daemon, remote, server, sm
    await daemon.stop()
    await engine.dispose()


async def open_console(remote):
    """Start a console's live stream and read the greeting."""
    stream = remote.stream()
    first = await stream.__anext__()
    assert first["topic"] == "modes"
    return stream


async def test_starts_paused_wakes_for_a_viewer_and_pauses_when_alone(split):
    daemon, remote, _, _ = split
    assert not daemon.host.running and daemon.host.paused_by == "idle"
    stream = await open_console(remote)
    await until(lambda: daemon.host.running)  # the first viewer woke the fab
    # Live events arrive in batches through Redis.
    topics = set()
    while not {"equipment", "activity"} <= topics:
        topics.add((await asyncio.wait_for(stream.__anext__(), 5))["topic"])
    await stream.aclose()  # console closed: viewer_left
    await until(lambda: not daemon.host.running, timeout=5)
    assert daemon.host.paused_by == "idle"


async def test_paused_fab_still_reports_tools_coming_online(split):
    _, remote, _, _ = split

    async def all_online():
        return len([e for e in await remote.equipment() if e["connected"]]) == 4

    await until(all_online)  # no console open, so the fab is paused the whole time
    assert (await remote.modes())["running"] is False


async def test_snapshots_serve_live_state(split):
    daemon, remote, _, _ = split
    stream = await open_console(remote)

    async def all_online():
        return len([e for e in await remote.equipment() if e["connected"]]) == 4

    await until(all_online)
    assert (await remote.modes())["running"] is True

    async def frame():
        return await remote.frame("ETCH01")

    async def detail():
        return await remote.equipment_detail("ETCH01")

    async def activity():
        return await remote.activity()

    assert (await until(frame))["sensors"]
    view = await until(detail)
    assert view["id"] == "ETCH01" and "trace" in view
    assert await until(activity)
    assert len(await remote.command("sim_tools")) == 4  # ground truth from the snapshot
    await stream.aclose()


async def test_commands_round_trip_through_the_daemon(split):
    daemon, remote, _, _ = split
    stream = await open_console(remote)
    await until(lambda: daemon.host.running)
    await until(lambda: all(l.connected for l in daemon.host.links.values()))

    modes = await remote.command("set_modes", predictive=False)
    assert modes["predictive"] is False and daemon.host.predictive is False

    wo = await remote.command("request_work_order", tool="CVD02", kind="PM")
    assert wo["kind"] == "PM" and daemon.host.links["CVD02"].work_order["id"] == wo["id"]

    status = await remote.command("sim", tool="ETCH01", action="wear", body={"wear": 0.9})
    assert status["wear"] == 0.9
    assert any(a["kind"] == "sim.wear" for a in daemon.host.activity_log)

    with pytest.raises(CommandError) as err:
        await remote.command("abort", tool="NOPE")
    assert err.value.status == 404
    with pytest.raises(CommandError) as err:
        await remote.command("sim", tool="ETCH01", action="wear", body={"wear": 5})
    assert err.value.status == 422
    await stream.aclose()


async def test_operator_pause_is_not_undone_by_viewers(split):
    daemon, remote, _, _ = split
    stream = await open_console(remote)
    await until(lambda: daemon.host.running)
    await remote.command("set_modes", running=False)
    assert daemon.host.paused_by == "operator"
    await remote.notify("viewer", id="someone-else")
    await asyncio.sleep(0.3)
    assert not daemon.host.running  # only an operator's start resumes it
    await stream.aclose()


async def test_command_without_a_daemon_says_so(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/none.db"
    engine = make_engine(url)
    remote = RemoteFab(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()), make_sessionmaker(engine))
    with pytest.raises(CommandError) as err:
        await remote.command("set_modes", predictive=True)
    assert err.value.status == 503
    await engine.dispose()


async def test_cloud_api_without_redis_says_what_is_missing():
    app = create_remote_app(Settings(redis_url=""))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/status")
    assert r.status_code == 503 and "REDIS_URL" in r.json()["detail"]


async def test_cloud_api_routes(split):
    daemon, _, server, sm = split
    app = create_remote_app(redis=fakeredis.FakeAsyncRedis(server=server), sessionmaker=sm)
    await daemon.handle({"cmd": "viewer", "args": {"id": "test"}})
    await until(lambda: all(l.connected for l in daemon.host.links.values()))
    await daemon.write_snapshot()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        status = (await c.get("/api/status")).json()
        assert status["equipment_online"] == 4 and status["capabilities"] == {"shutdown": False}
        assert len((await c.get("/api/equipment")).json()) == 4
        assert (await c.get("/api/equipment/NOPE")).status_code == 404
        assert (await c.get("/api/lots")).status_code == 200  # straight from the database
        assert (await c.put("/api/modes", json={"auto_lots": False})).json()["auto_lots"] is False
        assert (await c.post("/api/system/shutdown")).status_code == 403


async def test_prune_keeps_the_database_small(split):
    daemon, _, _, sm = split
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    async with sm() as s:
        s.add(EventLog(equipment_id="ETCH01", event="OLD", data={}, created_at=old))
        await s.commit()
    await daemon.prune()
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(EventLog).where(EventLog.event == "OLD")) == 0


@pytest.mark.parametrize(
    "given,expected",
    [
        (
            "postgres://u:p@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x-pooler.neon.tech/db?ssl=require",
        ),
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db?ssl=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        ("sqlite+aiosqlite:///./fab.db", "sqlite+aiosqlite:///./fab.db"),
    ],
)
def test_database_urls_from_providers(given, expected):
    assert normalize_database_url(given) == expected
