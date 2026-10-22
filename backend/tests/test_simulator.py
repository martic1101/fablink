import asyncio

from app.secs.messages import HCACK, ControlState, EquipmentState, parse_s6f11
from simulator.clock import SimClock
from simulator.crew import MaintenanceCrew
from simulator.tool import SimTool, default_fleet


def make_tool(**kw):
    spec = default_fleet(0)[0]
    spec.port = 0
    return SimTool(spec, speed=100, seed=1, **kw)


async def test_start_parameter_validation():
    t = make_tool()
    assert t.remote_command("START", {"LOTID": "L1"}) == HCACK.PARAMETER_INVALID
    for wafers in ("x", "0", "26"):
        params = {"LOTID": "L1", "RECIPE": "R", "WAFERS": wafers}
        assert t.remote_command("START", params) == HCACK.PARAMETER_INVALID
    assert t.remote_command("FLY", {}) == HCACK.INVALID_COMMAND


async def test_rejects_second_command_while_job_running():
    """Regression: a command accepted while a job is still running left several
    jobs fighting over the tool state."""
    t = make_tool()
    params = {"LOTID": "L1", "RECIPE": "R", "WAFERS": "5"}
    assert t.remote_command("START", params) == HCACK.ACCEPTED_WILL_FINISH_LATER
    assert t.remote_command("START", {**params, "LOTID": "L2"}) == HCACK.CANNOT_PERFORM_NOW
    await t.stop()


async def test_maintenance_is_not_a_remote_command():
    t = make_tool()
    assert t.remote_command("MAINT", {"KIND": "PM"}) == HCACK.INVALID_COMMAND


async def test_tool_in_local_control_refuses_remote_commands():
    t = make_tool()
    t.control_state = ControlState.LOCAL
    params = {"LOTID": "L1", "RECIPE": "R", "WAFERS": "5"}
    assert t.remote_command("START", params) == HCACK.CANNOT_PERFORM_NOW


async def test_technician_does_pm_locally_and_tool_reports_it():
    t = make_tool(initial_wear=0.6)
    crew = MaintenanceCrew({"ETCH01": t}, travel_seconds=1, speed=100)
    job = crew.accept(7, "ETCH01", "PM")
    assert crew.accept(7, "ETCH01", "PM") is job  # resending a work order is harmless
    await asyncio.wait_for(job.task, 2)
    assert job.status == "DONE" and t.wear == 0.0 and t.control_state == ControlState.REMOTE
    events = [parse_s6f11(body) for s, f, body in t._spool if (s, f) == (6, 11)]
    seq = [(e.ceid.name, e.values) for e in events]
    assert seq[:3] == [
        ("CONTROL_STATE_CHANGED", ["ONLINE_LOCAL"]),
        ("STATE_CHANGED", ["PM", "PM in progress, work order 7"]),
        ("MAINT_STARTED", ["PM", 7]),
    ]
    assert ("MAINT_COMPLETED", ["PM", 7]) in seq


async def test_repair_waits_for_a_down_tool_and_can_be_cancelled():
    t = make_tool()
    crew = MaintenanceCrew({"ETCH01": t}, travel_seconds=1, speed=100)
    job = crew.accept(8, "ETCH01", "REPAIR")
    await asyncio.sleep(0.1)
    assert job.status == "WAITING_FOR_TOOL"  # tool is IDLE, nothing to repair
    crew.cancel(8)
    assert job.status == "CANCELLED"
    await crew.stop()


async def test_worn_tool_fails_and_goes_down():
    t = make_tool(initial_wear=0.99)
    t.remote_command("START", {"LOTID": "L1", "RECIPE": "R", "WAFERS": "5"})
    await asyncio.sleep(0.2)
    assert t.state == EquipmentState.DOWN
    # Not connected to a host -> every report was spooled, nothing lost.
    names = [(s, f) for s, f, _ in t._spool]
    assert (5, 1) in names  # alarm
    assert names.count((6, 11)) >= 4


async def test_health_sensor_drifts_with_wear():
    fresh, worn = make_tool(initial_wear=0.0), make_tool(initial_wear=0.9)
    fresh.state = worn.state = EquipmentState.PROCESSING
    refl = lambda tool: sum(tool.read_sensors()[1] for _ in range(50)) / 50  # noqa: E731
    assert refl(worn) - refl(fresh) > 15


async def test_paused_clock_freezes_simulated_time():
    clock = SimClock()
    clock.pause()
    task = asyncio.ensure_future(clock.sleep(0.05))
    await asyncio.sleep(0.2)
    assert not task.done()  # no simulated time passes while paused
    clock.resume()
    await asyncio.wait_for(task, 1)
