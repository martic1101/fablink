import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import sim_proxy
from app.config import Settings
from app.main import create_app
from simulator.control import create_control_app
from simulator.tool import SimTool, default_fleet


@pytest.fixture
def tools():
    spec = default_fleet(0)[0]
    spec.port = 0
    return {"ETCH01": SimTool(spec, seed=1)}


def test_status_reports_ground_truth(tools):
    c = TestClient(create_control_app(tools))
    [st] = c.get("/tools").json()
    assert st["id"] == "ETCH01" and st["wear"] == 0.0 and st["spooled"] == 0 and not st["connected"]


def test_wear_and_degrade(tools):
    c = TestClient(create_control_app(tools))
    assert c.post("/tools/ETCH01/wear", json={"wear": 0.9}).json()["wear"] == 0.9
    assert c.post("/tools/ETCH01/degrade", json={"multiplier": 4}).json()["wear_multiplier"] == 4
    assert c.post("/tools/ETCH01/wear", json={"wear": 1.5}).status_code == 422
    assert c.post("/tools/NOPE/wear", json={"wear": 0.5}).status_code == 404


def test_crew_inbox(tools):
    c = TestClient(create_control_app(tools))
    assert (
        c.post("/crew/work-orders", json={"id": 3, "tool": "ETCH01", "kind": "PM"}).json()["status"]
        == "TRAVELLING"
    )
    assert c.post("/crew/work-orders", json={"id": 4, "tool": "NOPE", "kind": "PM"}).status_code == 404
    assert c.post("/crew/work-orders/3/cancel").json()["status"] == "CANCELLED"
    assert c.post("/crew/work-orders/99/cancel").status_code == 404


def test_clock_and_shutdown(tools):
    c = TestClient(create_control_app(tools))
    assert c.post("/clock/pause").json() == {"paused": True}
    assert c.get("/clock").json() == {"paused": True}
    assert c.post("/clock/resume").json() == {"paused": False}
    assert c.post("/shutdown").status_code == 501  # nothing to stop in this process
    stopped = []
    c2 = TestClient(create_control_app(tools, on_shutdown=lambda: stopped.append(True)))
    assert c2.post("/shutdown").json() == {"ok": True} and stopped == [True]


def test_disconnect_blocks_reconnects(tools):
    c = TestClient(create_control_app(tools))
    assert c.post("/tools/ETCH01/disconnect", json={"seconds": 5}).json()["blocked_for"] > 4


def _app(tmp_path, url):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/p.db",
        equipment=[("ETCH01", "127.0.0.1", 1)],
        start_background=False,
        sim_control_url=url,
    )
    return create_app(settings)


def test_proxy_reports_unreachable_simulator(tmp_path):
    with TestClient(_app(tmp_path, "http://127.0.0.1:1")) as c:
        r = c.get("/api/sim/tools")
        assert r.status_code == 503 and "not reachable" in r.json()["detail"]


def test_proxy_forwards_and_records_activity(tmp_path, tools, monkeypatch):
    control = create_control_app(tools)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        sim_proxy.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.ASGITransport(app=control), **kw),
    )
    with TestClient(_app(tmp_path, "http://sim")) as c:
        assert c.post("/api/sim/tools/ETCH01/wear", json={"wear": 0.995}).json()["wear"] == 0.995
        assert c.post("/api/sim/tools/ETCH01/explode", json={}).status_code == 404
        [entry] = c.get("/api/activity?block=simulator").json()
        assert entry["kind"] == "sim.wear" and "99.5%" in entry["text"]


def test_proxy_logs_action_before_its_effects(tmp_path, monkeypatch):
    """Regression: cutting the link makes the gateway log link.down before the
    simulator's HTTP reply returns. The scenario action must still come first."""

    async def fake_call(request, method, path, json=None):
        request.app.state.host.activity("gateway", "ETCH01", "link.down", "session closed")
        return {}

    monkeypatch.setattr(sim_proxy, "_call", fake_call)
    with TestClient(_app(tmp_path, "http://sim")) as c:
        c.post("/api/sim/tools/ETCH01/disconnect", json={"seconds": 5})
        seen = [e["kind"] for e in c.get("/api/activity?tool=ETCH01").json()]
        assert [k for k in seen if k in ("sim.disconnect", "link.down")] == ["sim.disconnect", "link.down"]
