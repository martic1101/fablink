from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..analytics import kpi
from ..config import RECIPES
from ..equipment.gateway import EquipmentLink
from ..host import OK_ACKS, FabHost
from ..models import Alarm, EventLog, Lot, Maintenance, StateHistory, WaferResult, iso
from ..secs.hsms import T3Timeout

router = APIRouter(prefix="/api")
TRACE_SF = ("S6F1", "S6F2")


def get_host(request: Request) -> FabHost:
    return request.app.state.host


def get_link(host: FabHost, tool_id: str) -> EquipmentLink:
    link = host.links.get(tool_id)
    if link is None:
        raise HTTPException(404, f"unknown equipment {tool_id}")
    return link


# ---- request bodies ---------------------------------------------------------------------------
class CommandIn(BaseModel):
    # The only remote command an operator sends from here. Maintenance is a
    # work order (below), carried out by technicians at the tool.
    command: str = Field(..., pattern="^ABORT$")


class WorkOrderIn(BaseModel):
    kind: str = Field(..., pattern="^(PM|REPAIR)$")


class LotIn(BaseModel):
    tool_type: str
    wafers: int = Field(..., ge=1, le=25)
    recipe: Optional[str] = None
    priority: int = Field(5, ge=1, le=9)


class ModesIn(BaseModel):
    running: Optional[bool] = None  # pause / start the simulation
    predictive: Optional[bool] = None
    auto_dispatch: Optional[bool] = None
    auto_work_orders: Optional[bool] = None
    auto_lots: Optional[bool] = None


# ---- equipment ------------------------------------------------------------------------------
@router.get("/status")
async def status(request: Request):
    host = get_host(request)
    return {
        "ok": True,
        "equipment_online": sum(1 for l in host.links.values() if l.connected),
        "equipment_total": len(host.links),
        "ws_clients": host.bus.subscriber_count,
    }


@router.get("/equipment")
async def list_equipment(request: Request):
    host = get_host(request)
    return [host.equipment_view(l) for l in host.links.values()]


@router.get("/equipment/{tool_id}")
async def equipment_detail(tool_id: str, request: Request):
    host = get_host(request)
    link = get_link(host, tool_id)
    view = host.equipment_view(link)
    view["trace"] = [{"ts": ts, "values": vals} for ts, vals in link.trace]
    return view


@router.get("/equipment/{tool_id}/frame")
async def equipment_frame(tool_id: str, request: Request):
    """The latest S6F1 trace message, as bytes on the wire, decoded SECS-II and
    named values — the three steps of reading data from a tool."""
    host = get_host(request)
    link = get_link(host, tool_id)
    if link.last_frame is None:
        raise HTTPException(404, "no trace data received from this tool yet")
    ts, msg = link.last_frame
    values = [item.scalar for item in msg.body[3].value]
    return {
        "ts": ts,
        "hex": msg.pack().hex(),
        "sml": msg.to_sml(),
        "sensors": [
            {"svid": svid, "name": name, "units": units, "value": val}
            for (svid, name, units), val in zip(link.sensors, values)
        ],
    }


@router.get("/equipment/{tool_id}/wafers")
async def equipment_wafers(tool_id: str, request: Request, limit: int = Query(100, le=1000)):
    host = get_host(request)
    get_link(host, tool_id)
    async with host.session() as s:
        rows = (
            await s.execute(
                select(WaferResult)
                .where(WaferResult.equipment_id == tool_id)
                .order_by(WaferResult.id.desc())
                .limit(limit)
            )
        ).scalars()
        return [
            {
                "lot_id": r.lot_id,
                "slot": r.slot,
                "result": r.result,
                "value": r.health_value,
                "predictive_mode": r.predictive_mode,
                "ts": iso(r.created_at),
            }
            for r in rows
        ][::-1]


@router.post("/equipment/{tool_id}/commands")
async def equipment_command(tool_id: str, body: CommandIn, request: Request):
    host = get_host(request)
    link = get_link(host, tool_id)
    try:
        ack = await host.abort(link)
    except T3Timeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if ack not in OK_ACKS:
        raise HTTPException(409, f"equipment rejected {body.command}: HCACK={ack.name}")
    return {"hcack": ack.name}


# ---- maintenance work orders ---------------------------------------------------------------------
@router.post("/equipment/{tool_id}/work-orders", status_code=201)
async def request_work_order(tool_id: str, body: WorkOrderIn, request: Request):
    host = get_host(request)
    link = get_link(host, tool_id)
    try:
        return await host.request_work_order(link, body.kind)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/work-orders")
async def list_work_orders(request: Request, limit: int = Query(30, le=200)):
    host = get_host(request)
    async with host.session() as s:
        rows = (await s.execute(select(Maintenance).order_by(Maintenance.id.desc()).limit(limit))).scalars()
        return [host.work_order_view(m) for m in rows]


@router.post("/work-orders/{wo_id}/send")
async def send_work_order(wo_id: int, request: Request):
    host = get_host(request)
    link = next((l for l in host.links.values() if l.work_order and l.work_order["id"] == wo_id), None)
    if link is None:
        raise HTTPException(404, "no active work order with that id")
    if not await host.send_work_order(link):
        raise HTTPException(409, "work order is not open, or the technicians could not be reached")
    return link.work_order


# ---- lots ---------------------------------------------------------------------------------------
@router.get("/lots")
async def list_lots(
    request: Request, status: Optional[List[str]] = Query(None), limit: int = Query(100, le=500)
):
    host = get_host(request)
    async with host.session() as s:
        q = select(Lot).order_by(Lot.created_at.desc()).limit(limit)
        if status:
            q = q.where(Lot.status.in_(status))
        return [host.lot_view(l) for l in (await s.execute(q)).scalars()]


@router.post("/lots", status_code=201)
async def create_lot(body: LotIn, request: Request):
    host = get_host(request)
    async with host.session() as s:
        try:
            lot = await host.lots.create(s, body.tool_type, body.wafers, body.recipe, priority=body.priority)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await s.flush()
        host.publish_lot(lot)
        return host.lot_view(lot)


@router.post("/lots/{lot_id}/release")
async def release_lot(lot_id: str, request: Request):
    host = get_host(request)
    async with host.session() as s:
        lot = await s.get(Lot, lot_id)
        if lot is None:
            raise HTTPException(404, "lot not found")
        try:
            await host.lots.release(s, lot)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        host.publish_lot(lot)
        return host.lot_view(lot)


@router.get("/recipes")
async def recipes():
    return RECIPES


# ---- alarms / events / logs ------------------------------------------------------------------
@router.get("/alarms")
async def list_alarms(request: Request, active: bool = False, limit: int = Query(100, le=500)):
    host = get_host(request)
    async with host.session() as s:
        q = select(Alarm).order_by(Alarm.id.desc()).limit(limit)
        if active:
            q = q.where(Alarm.cleared_at.is_(None))
        return [
            {
                "id": a.id,
                "tool": a.equipment_id,
                "alid": a.alid,
                "text": a.text,
                "severity": a.severity,
                "set_at": iso(a.set_at),
                "cleared_at": iso(a.cleared_at),
            }
            for a in (await s.execute(q)).scalars()
        ]


@router.get("/events")
async def list_events(request: Request, tool: Optional[str] = None, limit: int = Query(100, le=1000)):
    host = get_host(request)
    async with host.session() as s:
        q = select(EventLog).order_by(EventLog.id.desc()).limit(limit)
        if tool:
            q = q.where(EventLog.equipment_id == tool)
        return [
            {"tool": e.equipment_id, "event": e.event, "data": e.data, "ts": iso(e.created_at)}
            for e in (await s.execute(q)).scalars()
        ]


@router.get("/secs-log")
async def secs_log(
    request: Request, tool: Optional[str] = None, include_trace: bool = False, limit: int = 100
):
    host = get_host(request)
    rows = [
        e
        for e in host.secs_log
        if (tool is None or e["tool"] == tool) and (include_trace or e["sf"] not in TRACE_SF)
    ]
    return rows[-limit:]


@router.get("/activity")
async def activity(
    request: Request, tool: Optional[str] = None, block: Optional[str] = None, limit: int = Query(200, le=600)
):
    host = get_host(request)
    rows = [
        e
        for e in host.activity_log
        if (tool is None or e["tool"] == tool) and (block is None or e["block"] == block)
    ]
    return rows[-limit:]


@router.get("/system")
async def system(request: Request):
    """Row counts per table and live connection numbers for the System map."""
    host = get_host(request)
    async with host.session() as s:
        tables = {}
        for name, model in (
            ("lots", Lot),
            ("wafer_results", WaferResult),
            ("alarms", Alarm),
            ("maintenance", Maintenance),
            ("state_history", StateHistory),
            ("event_log", EventLog),
        ):
            tables[name] = await s.scalar(select(func.count()).select_from(model))
    return {
        "tables": tables,
        "ws_clients": host.bus.subscriber_count,
        "equipment_online": sum(1 for l in host.links.values() if l.connected),
        "secs_messages_logged": len(host.secs_log),
    }


# ---- KPIs and modes -------------------------------------------------------------------------
@router.get("/kpi")
async def get_kpi(request: Request):
    host = get_host(request)
    async with host.session() as s:
        return {
            "availability": await kpi.availability(s),
            "strategies": await kpi.strategy_comparison(s),
        }


@router.get("/modes")
async def get_modes(request: Request):
    return get_host(request).modes()


@router.put("/modes")
async def set_modes(body: ModesIn, request: Request):
    host = get_host(request)
    changes = body.model_dump(exclude_none=True)
    running = changes.pop("running", None)
    for key, value in changes.items():
        setattr(host, key, value)
    if running is not None and running != host.running:
        await host.set_running(running)
    host.bus.publish("modes", host.modes())
    return host.modes()


@router.post("/system/shutdown")
async def shutdown(request: Request):
    """Stop the simulator and this host (the console's "Shut down" button)."""
    host = get_host(request)
    if not host.settings.allow_shutdown:
        raise HTTPException(403, "Shutting down from the console is disabled (ALLOW_SHUTDOWN=0).")
    host.activity("api", None, "system.shutdown", "Shutdown requested from the console.")
    host.bus.publish("shutdown", {})
    if host.simulator:
        try:
            await host.simulator.shutdown()
        except Exception:
            pass  # simulator already gone; still stop ourselves
    request.app.state.request_exit()
    return {"ok": True}


# ---- WebSocket ----------------------------------------------------------------------------------
@router.websocket("/ws")
async def ws(websocket: WebSocket):
    host: FabHost = websocket.app.state.host
    await websocket.accept()
    q = host.bus.subscribe()
    try:
        await websocket.send_json({"topic": "modes", "data": host.modes()})
        for link in host.links.values():
            await websocket.send_json({"topic": "equipment", "data": host.equipment_view(link)})
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        host.bus.unsubscribe(q)
