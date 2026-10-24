"""End-to-end: simulated tools <-HSMS-> FabHost <-> SQLite, all in one event loop."""

import asyncio
from typing import List

import pytest
from sqlalchemy import func, select

from app.bus import EventBus
from app.config import Settings
from app.db import create_schema, make_engine, make_sessionmaker
from app.host import FabHost
from app.models import Alarm, EventLog, Lot, LotStatus, Maintenance, WaferResult, WorkOrderStatus
from app.secs.hsms import SecsMessage
from app.secs.messages import CEID, s6f11_body
from simulator.clock import SimClock
from simulator.crew import MaintenanceCrew
from simulator.tool import SimTool, default_fleet

pytestmark = pytest.mark.integration

SPEED = 40


async def wait_for(cond, timeout=10.0, interval=0.02):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


class InProcessSink:
    """Hands work orders straight to the simulated crew (the demo goes over HTTP)."""

    def __init__(self, crew: MaintenanceCrew) -> None:
        self.crew = crew

    async def send(self, wo):
        self.crew.accept(wo["id"], wo["tool"], wo["kind"])

    async def cancel(self, wo_id):
        self.crew.cancel(wo_id)


class InProcessSimulator:
    """The host's simulator controls, pointed at an in-process clock."""

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock

    async def pause(self):
        self.clock.pause()

    async def resume(self):
        self.clock.resume()

    async def is_paused(self):
        return self.clock.paused

    async def shutdown(self):
        pass


@pytest.fixture
async def fab(tmp_path):
    created: List = []

    async def factory(wear=(0.0,), tools=None, **host_kw):
        specs = tools or [default_fleet(0)[0]]
        clock = SimClock()
        sims = []
        for spec, w in zip(specs, wear):
            spec.port = 0
            sim = SimTool(spec, speed=SPEED, initial_wear=w, seed=42, clock=clock)
            await sim.serve()
            sims.append(sim)
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
            equipment=[(s.spec.tool_id, "127.0.0.1", s.spec.port) for s in sims],
            dispatch_interval=0.02,
            wip_target=2,
            t3_seconds=2,
        )
        engine = make_engine(settings.database_url)
        await create_schema(engine)
        crew = MaintenanceCrew({s.spec.tool_id: s for s in sims}, travel_seconds=4, speed=SPEED, clock=clock)
        host = FabHost(
            settings,
            make_sessionmaker(engine),
            EventBus(),
            work_order_sink=InProcessSink(crew),
            simulator=InProcessSimulator(clock),
        )
        for k, v in host_kw.items():
            setattr(host, k, v)
        await host.start()
        created.append((host, sims, engine, crew))
        await wait_for(lambda: _all_online(host))
        return host, sims

    yield factory
    for host, sims, engine, crew in created:
        await crew.stop()
        await host.stop()
        for s in sims:
            await s.stop()
        await engine.dispose()


async def _all_online(host):
    return all(l.connected for l in host.links.values())


async def count(host, model, *where):
    async with host.session() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


async def test_discovery_and_lot_completes(fab):
    host, _ = await fab()
    link = host.links["ETCH01"]
    assert link.tool_type == "ETCH" and link.mdln == "ETCH-9000"
    assert [n for _, n, _ in link.sensors][:2] == ["RF_FWD_POWER", "RF_REFL_POWER"]

    await wait_for(lambda: count(host, Lot, Lot.status == LotStatus.COMPLETED), timeout=15)
    async with host.session() as s:
        lot = (await s.execute(select(Lot).where(Lot.status == LotStatus.COMPLETED))).scalars().first()
        wafers = (await s.execute(select(WaferResult).where(WaferResult.lot_id == lot.id))).scalars().all()
    assert lot.wafers_done == lot.wafers == len(wafers)
    assert all(w.health_value is not None for w in wafers)  # FDC feature from trace


async def test_reactive_failure_holds_lot_and_repairs_exactly_once(fab):
    """Regression for the double-REPAIR race (see docs/engineering-notes.md)."""
    host, sims = await fab(wear=(0.95,), predictive=False)
    fault = sims[0].spec.fault_alid
    await wait_for(lambda: count(host, Alarm, Alarm.alid == fault), timeout=10)
    await wait_for(lambda: count(host, EventLog, EventLog.event == "MAINT_COMPLETED"), timeout=10)
    host.auto_dispatch = False  # freeze wear so the assertions below don't race new lots
    await asyncio.sleep(0.3)  # give the dispatcher a few cycles to misbehave
    assert await count(host, Maintenance, Maintenance.kind == "REPAIR") == 1
    assert await count(host, EventLog, EventLog.event == "MAINT_STARTED") == 1
    assert await count(host, Lot, Lot.status == LotStatus.HOLD) == 1
    # Only the fault alarm: random "robot retry" nuisance alarms may be active at any time.
    assert await count(host, Alarm, Alarm.alid == fault, Alarm.cleared_at.is_(None)) == 0
    assert sims[0].wear < 0.2


async def test_predictive_mode_schedules_pm_before_failure(fab):
    host, sims = await fab(wear=(0.7,), predictive=True)
    done_pm = (
        Maintenance.kind == "PM",
        Maintenance.trigger == "PREDICTIVE",
        Maintenance.status == WorkOrderStatus.DONE,
    )
    await wait_for(lambda: count(host, Maintenance, *done_pm), timeout=20)
    assert await count(host, Maintenance, Maintenance.kind == "REPAIR") == 0
    assert await count(host, Alarm, Alarm.alid == sims[0].spec.fault_alid) == 0
    # The tool was held: no lot was dispatched between raising the work order
    # and the technician finishing, and the PM was reported by the tool itself.
    seq = kinds(host, "ETCH01")
    held = seq[seq.index("wo.opened") : seq.index("maint.completed")]
    assert "dispatch.lot" not in held
    assert in_order(held, ["wo.sent", "control", "maint.started"])


async def test_reconnect_and_spooled_events_after_link_loss(fab):
    host, sims = await fab(auto_dispatch=False, auto_lots=False)
    sim, link = sims[0], host.links["ETCH01"]
    await sim.conn.close()  # network blip
    await wait_for(lambda: _is(lambda: not link.connected))
    # While offline the tool raises an alarm -> must be spooled, then delivered.
    sim._alarm(1234, "spooled while offline", True, severity=5)
    assert len(sim._spool) == 1
    await wait_for(lambda: _is(lambda: link.connected), timeout=10)
    await wait_for(lambda: count(host, Alarm, Alarm.alid == 1234))
    # The tool pops a spooled report only after the host's reply arrives, which
    # is after the host has stored it, so wait rather than assert.
    await wait_for(lambda: _is(lambda: len(sim._spool) == 0))


async def _is(fn):
    return fn()


async def test_released_hold_lot_only_runs_remaining_wafers(fab):
    host, sims = await fab(auto_dispatch=False, auto_lots=False)
    link = host.links["ETCH01"]
    async with host.session() as s:
        lot = await host.lots.create(s, "ETCH", 10)
        lot.status, lot.wafers_done, lot.hold_reason = LotStatus.HOLD, 4, "tool fault"
        await host.lots.release(s, lot)
        lot_id = lot.id
    async with host.session() as s:
        lot = await s.get(Lot, lot_id)
        await host.start_lot(link, lot)
    await wait_for(lambda: count(host, Lot, Lot.id == lot_id, Lot.status == LotStatus.COMPLETED))
    async with host.session() as s:
        lot = await s.get(Lot, lot_id)
    assert (lot.wafers_done, lot.wafers) == (10, 10)
    assert await count(host, WaferResult, WaferResult.lot_id == lot_id) == 6


async def test_health_model_survives_host_restart(fab, tmp_path):
    """A restarted host must remember tool wear, or it would dispatch lots onto
    tools that are about to fail."""
    host, _ = await fab(wear=(0.5,), predictive=False)
    await wait_for(lambda: count(host, WaferResult), timeout=10)
    await wait_for(lambda: _is(lambda: len(host.models["ETCH01"].history) >= 6), timeout=10)
    before = host.models["ETCH01"].history[-1]
    await host.stop()

    restarted = FabHost(host.settings, host._sessionmaker, EventBus())
    restarted.auto_dispatch = False
    await restarted.start()
    try:
        await wait_for(lambda: _all_online(restarted))
        model = restarted.models["ETCH01"]
        assert len(model.history) >= 6
        assert before in model.history
    finally:
        await restarted.stop()


def kinds(host, tool, since=0):
    return [e["kind"] for e in host.activity_log if e["tool"] == tool and e["id"] > since]


def in_order(seq, expected):
    """True if ``expected`` appears in ``seq`` as a subsequence."""
    it = iter(seq)
    return all(any(k == want for k in it) for want in expected)


async def test_wafer_pipeline_activity_in_order(fab):
    host, _ = await fab()
    steps = [
        "wafer.started",
        "trace.first",
        "wafer.completed",
        "analytics.feature",
        "analytics.health",
        "db.wafer",
        "ws.pushed",
    ]
    await wait_for(lambda: _is(lambda: in_order(kinds(host, "ETCH01"), steps)), timeout=10)


async def test_sudden_failure_activity(fab):
    host, sims = await fab(predictive=False)
    await wait_for(lambda: _is(lambda: sims[0].state == "PROCESSING"))
    mark = host._activity_seq
    sims[0].wear = 0.995
    steps = [
        "alarm.set",
        "lot.hold",
        "state",
        "wo.opened",
        "repair.opened",
        "wo.sent",
        "control",
        "maint.started",
        "alarm.cleared",
        "maint.completed",
    ]
    await wait_for(lambda: _is(lambda: in_order(kinds(host, "ETCH01", mark), steps)), timeout=10)


async def test_network_loss_spools_and_replays(fab):
    host, sims = await fab()
    sim, link = sims[0], host.links["ETCH01"]
    await wait_for(lambda: _is(lambda: sim.state == "PROCESSING"))
    mark = host._activity_seq
    await sim.disconnect(1.0)
    await wait_for(lambda: _is(lambda: not link.connected))
    await wait_for(lambda: _is(lambda: len(sim._spool) > 0), timeout=5)
    await wait_for(lambda: _is(lambda: "spool.replayed" in kinds(host, "ETCH01", mark)), timeout=10)
    assert in_order(kinds(host, "ETCH01", mark), ["link.down", "link.retry", "link.up", "spool.replayed"])
    replay = [e for e in host.activity_log if e["kind"] == "spool.replayed" and e["id"] > mark][0]
    assert replay["data"]["count"] > 0
    await wait_for(lambda: _is(lambda: len(sim._spool) == 0))


async def test_host_ignores_resent_events(fab):
    """At-least-once delivery means the tool may resend an event we already
    processed (its T3 expired before our reply arrived). It must count once."""
    host, _ = await fab(auto_dispatch=False, auto_lots=False)
    link = host.links["ETCH01"]
    msg = SecsMessage(6, 11, s6f11_body(987654, CEID.WAFER_COMPLETED, ["LOTX", 1, "OK"]), w_bit=True)
    first = await host._on_message(link, msg)
    second = await host._on_message(link, msg)
    assert first.name == second.name == "S6F12"  # both acknowledged
    assert await count(host, WaferResult, WaferResult.lot_id == "LOTX") == 1
    assert "event.duplicate" in kinds(host, "ETCH01")


async def test_manual_work_order_waits_for_engineer(fab):
    host, sims = await fab(auto_work_orders=False, auto_lots=False, auto_dispatch=False)
    link = host.links["ETCH01"]
    wo = await host.request_work_order(link, "PM")
    await asyncio.sleep(0.3)
    assert link.work_order["status"] == WorkOrderStatus.OPEN  # nobody sent it
    assert await host.send_work_order(link)
    await wait_for(lambda: count(host, Maintenance, Maintenance.id == wo["id"], Maintenance.status == "DONE"))
    assert link.work_order is None and sims[0].control_state == "ONLINE_REMOTE"


async def test_lost_reply_resend_is_dropped_by_host(fab):
    host, sims = await fab(predictive=False)
    sim = sims[0]
    await wait_for(lambda: _is(lambda: sim.state == "PROCESSING"))
    mark = host._activity_seq
    sim.lose_next_reply = True
    await wait_for(lambda: _is(lambda: "event.duplicate" in kinds(host, "ETCH01", mark)), timeout=10)
    assert sim.retries >= 1


async def test_pause_freezes_tools_and_dispatcher(fab):
    host, sims = await fab()
    sim, link = sims[0], host.links["ETCH01"]
    await wait_for(lambda: _is(lambda: sim.state == "PROCESSING"))
    await host.set_running(False)
    await asyncio.sleep(0.3)  # let anything already in flight land
    events, lots = sim._dataid, await count(host, Lot, Lot.status != LotStatus.QUEUED)
    await asyncio.sleep(0.6)  # many wafers' worth of time at 40x
    assert sim._dataid == events  # no simulated progress
    assert await count(host, Lot, Lot.status != LotStatus.QUEUED) == lots  # nothing dispatched
    assert link.connected  # the HSMS link stays up while paused
    await host.set_running(True)
    await wait_for(lambda: _is(lambda: sim._dataid > events))


async def test_stop_lets_in_flight_work_finish(fab):
    """Regression: stop() used to cancel the dispatcher and message handlers in
    the middle of database work, which could leave an aiosqlite connection that
    never finished closing and hang the next teardown (engineering notes #11)."""
    host, sims = await fab()
    await wait_for(lambda: _is(lambda: sims[0].state == "PROCESSING"))
    await host.stop()
    leftover = [
        t
        for t in asyncio.all_tasks()
        if not t.done()
        and any(k in repr(t) for k in ("FabHost._dispatch_loop", "EquipmentLink._run", "_terminate_graceful"))
    ]
    assert leftover == []
    assert not host.links["ETCH01"].conn._handlers  # every handler ran to completion
