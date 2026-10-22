import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    # Equipment points at a closed port: API must work with every tool offline.
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/api.db",
        equipment=[("ETCH01", "127.0.0.1", 1)],
        start_background=False,
        # A dead port, so tests can never pause or stop a simulator you have running.
        sim_control_url="http://127.0.0.1:1",
    )
    app = create_app(settings)
    app.state.exit_requests = []
    with TestClient(app) as c:
        app.state.request_exit = lambda: app.state.exit_requests.append(True)
        yield c


def test_status_and_equipment(client):
    assert client.get("/api/status").json()["equipment_online"] == 0
    eq = client.get("/api/equipment").json()
    assert eq[0]["id"] == "ETCH01" and eq[0]["state"] == "OFFLINE"
    assert client.get("/api/equipment/NOPE").status_code == 404


def test_create_and_list_lots(client):
    r = client.post("/api/lots", json={"tool_type": "ETCH", "wafers": 25, "priority": 1})
    assert r.status_code == 201
    lot = r.json()
    assert lot["status"] == "QUEUED" and lot["created_at"].endswith("+00:00")
    assert [l["id"] for l in client.get("/api/lots?status=QUEUED").json()] == [lot["id"]]


@pytest.mark.parametrize(
    "body",
    [
        {"tool_type": "ETCH", "wafers": 26},
        {"tool_type": "LITHO", "wafers": 5},
        {"tool_type": "ETCH", "wafers": 5, "recipe": "CVD_TEOS_01"},  # not qualified on ETCH
    ],
)
def test_lot_validation(client, body):
    assert client.post("/api/lots", json=body).status_code == 422


def test_release_requires_hold(client):
    lot = client.post("/api/lots", json={"tool_type": "CVD", "wafers": 3}).json()
    assert client.post(f"/api/lots/{lot['id']}/release").status_code == 409


def test_command_to_offline_tool_is_conflict(client):
    r = client.post("/api/equipment/ETCH01/commands", json={"command": "ABORT"})
    assert r.status_code == 409 and "offline" in r.json()["detail"]
    # Maintenance is not a remote command any more; it is a work order.
    assert client.post("/api/equipment/ETCH01/commands", json={"command": "PM"}).status_code == 422


def test_work_order_holds_tool(client):
    r = client.post("/api/equipment/ETCH01/work-orders", json={"kind": "PM"})
    assert r.status_code == 201
    wo = r.json()
    assert wo["status"] == "OPEN" and wo["trigger"] == "MANUAL"
    assert client.get("/api/equipment/ETCH01").json()["work_order"]["id"] == wo["id"]
    assert [w["id"] for w in client.get("/api/work-orders").json()] == [wo["id"]]
    # A repair needs a tool that is actually down.
    assert client.post("/api/equipment/ETCH01/work-orders", json={"kind": "REPAIR"}).status_code == 409


def test_modes_roundtrip(client):
    assert client.put("/api/modes", json={"predictive": False}).json()["predictive"] is False
    assert client.get("/api/modes").json()["predictive"] is False


def test_kpi_empty(client):
    k = client.get("/api/kpi").json()
    assert {s["mode"] for s in k["strategies"]} == {"predictive", "reactive"}


def test_websocket_sends_snapshot(client):
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["topic"] == "modes"
        assert ws.receive_json()["data"]["id"] == "ETCH01"


def test_system_activity_and_frame(client):
    sysinfo = client.get("/api/system").json()
    assert set(sysinfo["tables"]) >= {"lots", "wafer_results", "alarms", "maintenance"}
    assert isinstance(client.get("/api/activity").json(), list)
    assert client.get("/api/equipment/ETCH01/frame").status_code == 404  # no trace yet


def test_pause_and_start_simulation(client):
    assert client.get("/api/modes").json()["running"] is True
    assert client.put("/api/modes", json={"running": False}).json()["running"] is False
    # Simulator unreachable here: the dispatcher still pauses, and the log says why.
    [entry] = [a for a in client.get("/api/activity").json() if a["kind"] == "sim.clock"]
    assert "could not be reached" in entry["text"]
    assert client.put("/api/modes", json={"running": True}).json()["running"] is True


def test_shutdown_from_console(client):
    assert client.post("/api/system/shutdown").json() == {"ok": True}
    assert client.app.state.exit_requests == [True]
    client.app.state.host.settings.allow_shutdown = False
    assert client.post("/api/system/shutdown").status_code == 403
