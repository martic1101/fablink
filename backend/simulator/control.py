"""Control API for the simulated fab.

* ``/tools/...``: scenario controls (demo only, real tools have no such back
  door). The FabLink host proxies them under /api/sim so the System map can
  trigger scenarios and show the simulator's ground truth.
* ``/crew/...``: the simulated technicians' work-order inbox. The host sends
  work orders here, standing in for a real maintenance system.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .clock import SimClock
from .crew import MaintenanceCrew
from .tool import SimTool


class WearIn(BaseModel):
    wear: float = Field(..., ge=0.0, le=0.999)


class DegradeIn(BaseModel):
    multiplier: float = Field(..., ge=1.0, le=20.0)


class DisconnectIn(BaseModel):
    seconds: float = Field(..., ge=1.0, le=120.0)


class WorkOrderIn(BaseModel):
    id: int
    tool: str
    kind: str = Field(..., pattern="^(PM|REPAIR)$")


def create_control_app(
    tools: Dict[str, SimTool],
    crew: Optional[MaintenanceCrew] = None,
    clock: Optional[SimClock] = None,
    on_shutdown: Optional[Callable[[], None]] = None,
) -> FastAPI:
    app = FastAPI(title="FabLink simulator control")
    crew = crew or MaintenanceCrew(tools)
    clock = clock or crew.clock

    def get(tool_id: str) -> SimTool:
        tool = tools.get(tool_id)
        if tool is None:
            raise HTTPException(404, f"unknown tool {tool_id}")
        return tool

    @app.get("/tools")
    async def list_tools():
        return [t.status() for t in tools.values()]

    @app.post("/tools/{tool_id}/wear")
    async def set_wear(tool_id: str, body: WearIn):
        """Jump the consumable part's wear, e.g. 0.995 = fails on the next wafer."""
        tool = get(tool_id)
        tool.wear = body.wear
        return tool.status()

    @app.post("/tools/{tool_id}/degrade")
    async def degrade(tool_id: str, body: DegradeIn):
        """Make the part wear faster until the next maintenance (e.g. a bad batch)."""
        tool = get(tool_id)
        tool.wear_multiplier = body.multiplier
        return tool.status()

    @app.post("/tools/{tool_id}/disconnect")
    async def disconnect(tool_id: str, body: DisconnectIn):
        tool = get(tool_id)
        await tool.disconnect(body.seconds)
        return tool.status()

    @app.post("/tools/{tool_id}/lose-reply")
    async def lose_reply(tool_id: str):
        """The host's reply to the tool's next report is "lost": the tool times
        out and resends a report the host has already processed."""
        tool = get(tool_id)
        tool.lose_next_reply = True
        return tool.status()

    @app.get("/clock")
    async def clock_state():
        return {"paused": clock.paused}

    @app.post("/clock/pause")
    async def pause():
        """Freeze simulated time: wafers, maintenance and travel stop; links stay up."""
        clock.pause()
        return {"paused": True}

    @app.post("/clock/resume")
    async def resume():
        clock.resume()
        return {"paused": False}

    @app.post("/shutdown")
    async def shutdown():
        if on_shutdown is None:
            raise HTTPException(501, "shutdown is not available in this process")
        on_shutdown()
        return {"ok": True}

    @app.get("/crew")
    async def crew_status():
        return crew.status()

    @app.post("/crew/work-orders")
    async def accept_work_order(body: WorkOrderIn):
        get(body.tool)
        return crew.accept(body.id, body.tool, body.kind).view()

    @app.post("/crew/work-orders/{work_order}/cancel")
    async def cancel_work_order(work_order: int):
        job = crew.cancel(work_order)
        if job is None:
            raise HTTPException(404, f"unknown work order {work_order}")
        return job.view()

    return app
