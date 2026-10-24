"""The console's REST + WebSocket API.

Routes only talk to the ``Fab`` interface (app/fab.py), so the same API is
served by a single process with the fab inside it, or by the stateless cloud
API with the fab in a separate daemon.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..analytics import kpi
from ..commands import CommandError
from ..config import RECIPES
from ..fab import Fab, LocalFab
from ..host import FabHost
from ..models import Alarm, EventLog, Lot, Maintenance, StateHistory, WaferResult, iso

router = APIRouter(prefix="/api")
TRACE_SF = ("S6F1", "S6F2")


def get_fab(request: Request) -> Fab:
    fab = request.app.state.fab
    if fab is None:
        raise HTTPException(503, "REDIS_URL (or KV_URL) is not set, so the API can't reach the fab daemon.")
    return fab


async def run(fab: Fab, name: str, **args: Any) -> Any:
    """Run a fab command and turn its failure into the matching HTTP error."""
    try:
        return await fab.command(name, **args)
    except CommandError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


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
    fab = get_fab(request)
    st = await fab.status()
    return {
        "ok": True,
        "equipment_online": st["equipment_online"],
        "equipment_total": st["equipment_total"],
        "ws_clients": st["ws_clients"],
        "capabilities": fab.capabilities,
    }


@router.get("/equipment")
async def list_equipment(request: Request):
    return await get_fab(request).equipment()


@router.get("/equipment/{tool_id}")
async def equipment_detail(tool_id: str, request: Request):
    view = await get_fab(request).equipment_detail(tool_id)
    if view is None:
        raise HTTPException(404, f"unknown equipment {tool_id}")
    return view


@router.get("/equipment/{tool_id}/frame")
async def equipment_frame(tool_id: str, request: Request):
    """The latest S6F1 trace message, as bytes on the wire, decoded SECS-II and
    named values: the three steps of reading data from a tool."""
    frame = await get_fab(request).frame(tool_id)
    if frame is None:
        raise HTTPException(404, "no trace data received from this tool yet")
    return frame


@router.get("/equipment/{tool_id}/wafers")
async def equipment_wafers(tool_id: str, request: Request, limit: int = Query(100, le=1000)):
    async with get_fab(request).session() as s:
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
    return await run(get_fab(request), "abort", tool=tool_id)


# ---- maintenance work orders ---------------------------------------------------------------------
@router.post("/equipment/{tool_id}/work-orders", status_code=201)
async def request_work_order(tool_id: str, body: WorkOrderIn, request: Request):
    return await run(get_fab(request), "request_work_order", tool=tool_id, kind=body.kind)


@router.get("/work-orders")
async def list_work_orders(request: Request, limit: int = Query(30, le=200)):
    async with get_fab(request).session() as s:
        rows = (await s.execute(select(Maintenance).order_by(Maintenance.id.desc()).limit(limit))).scalars()
        return [FabHost.work_order_view(m) for m in rows]


@router.post("/work-orders/{wo_id}/send")
async def send_work_order(wo_id: int, request: Request):
    return await run(get_fab(request), "send_work_order", wo_id=wo_id)


# ---- lots ---------------------------------------------------------------------------------------
@router.get("/lots")
async def list_lots(
    request: Request, status: Optional[List[str]] = Query(None), limit: int = Query(100, le=500)
):
    async with get_fab(request).session() as s:
        q = select(Lot).order_by(Lot.created_at.desc()).limit(limit)
        if status:
            q = q.where(Lot.status.in_(status))
        return [FabHost.lot_view(l) for l in (await s.execute(q)).scalars()]


@router.post("/lots", status_code=201)
async def create_lot(body: LotIn, request: Request):
    return await run(
        get_fab(request),
        "create_lot",
        tool_type=body.tool_type,
        wafers=body.wafers,
        recipe=body.recipe,
        priority=body.priority,
    )


@router.post("/lots/{lot_id}/release")
async def release_lot(lot_id: str, request: Request):
    return await run(get_fab(request), "release_lot", lot_id=lot_id)


@router.get("/recipes")
async def recipes():
    return RECIPES


# ---- alarms / events / logs ------------------------------------------------------------------
@router.get("/alarms")
async def list_alarms(request: Request, active: bool = False, limit: int = Query(100, le=500)):
    async with get_fab(request).session() as s:
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
    async with get_fab(request).session() as s:
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
    rows = [
        e
        for e in await get_fab(request).secs_log()
        if (tool is None or e["tool"] == tool) and (include_trace or e["sf"] not in TRACE_SF)
    ]
    return rows[-limit:]


@router.get("/activity")
async def activity(
    request: Request, tool: Optional[str] = None, block: Optional[str] = None, limit: int = Query(200, le=600)
):
    rows = [
        e
        for e in await get_fab(request).activity()
        if (tool is None or e["tool"] == tool) and (block is None or e["block"] == block)
    ]
    return rows[-limit:]


@router.get("/system")
async def system(request: Request):
    """Row counts per table and live connection numbers for the System map."""
    fab = get_fab(request)
    tables: Dict[str, int] = {}
    async with fab.session() as s:
        for name, model in (
            ("lots", Lot),
            ("wafer_results", WaferResult),
            ("alarms", Alarm),
            ("maintenance", Maintenance),
            ("state_history", StateHistory),
            ("event_log", EventLog),
        ):
            tables[name] = await s.scalar(select(func.count()).select_from(model)) or 0
    st = await fab.status()
    return {
        "tables": tables,
        "ws_clients": st["ws_clients"],
        "equipment_online": st["equipment_online"],
        "secs_messages_logged": st["secs_messages_logged"],
    }


# ---- KPIs and modes -------------------------------------------------------------------------
@router.get("/kpi")
async def get_kpi(request: Request):
    async with get_fab(request).session() as s:
        return {
            "availability": await kpi.availability(s),
            "strategies": await kpi.strategy_comparison(s),
        }


@router.get("/modes")
async def get_modes(request: Request):
    return await get_fab(request).modes()


@router.put("/modes")
async def set_modes(body: ModesIn, request: Request):
    return await run(get_fab(request), "set_modes", **body.model_dump(exclude_none=True))


# ---- simulator (demo only) --------------------------------------------------------------------
@router.get("/sim/tools")
async def sim_tools(request: Request):
    """The simulator's ground truth (wear, spool), shown next to what the host inferred."""
    return await run(get_fab(request), "sim_tools")


@router.post("/sim/tools/{tool_id}/{action}")
async def sim_action(tool_id: str, action: str, request: Request, body: Optional[Dict[str, Any]] = None):
    """Scenario controls. Real equipment has no such back door."""
    return await run(get_fab(request), "sim", tool=tool_id, action=action, body=body or {})


@router.post("/system/shutdown")
async def shutdown(request: Request):
    """Stop the simulator and this host (the console's "Shut down" button, local only)."""
    fab = get_fab(request)
    if not fab.capabilities.get("shutdown") or not isinstance(fab, LocalFab):
        raise HTTPException(403, "Shutting down from the console is disabled here.")
    host = fab.host
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
    fab: Optional[Fab] = websocket.app.state.fab
    if fab is None:
        await websocket.close(code=1011)  # same cause as get_fab's 503
        return
    await websocket.accept()
    stream = fab.stream()
    try:
        async for msg in stream:
            await websocket.send_json(msg)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError, ConnectionError):
        pass
    finally:
        await stream.aclose()
