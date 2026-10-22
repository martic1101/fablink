"""Proxy to the simulator's scenario control API (demo only).

A real deployment has no simulator. The proxy exists so the browser talks to a
single origin, and so every scenario action also lands in the host's activity
stream, where the System map can show it next to the host's reaction.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..host import FabHost

router = APIRouter(prefix="/api/sim")

ACTIONS: Dict[str, str] = {
    "wear": "wear",
    "degrade": "degrade",
    "disconnect": "disconnect",
    "lose_reply": "lose-reply",
}


def _describe(action: str, body: Dict[str, Any]) -> str:
    if action == "wear":
        return (
            f"Simulator: consumable wear set to {body['wear']:.1%}. "
            "The host is not told; it can only see the sensors."
        )
    if action == "degrade":
        return (
            f"Simulator: part now wears {body['multiplier']:g}x faster until the next maintenance "
            "(e.g. a bad batch of parts). The host is not told."
        )
    if action == "lose_reply":
        return (
            "Simulator: the host's reply to the tool's next report will be lost on the way back, "
            "so the tool will time out (T3) and send that report again."
        )
    return (
        f"Simulator: network to the host dropped; new connections refused for {body['seconds']:g} s. "
        "The tool keeps processing and spools its reports."
    )


async def _call(request: Request, method: str, path: str, json: Any = None) -> Any:
    host: FabHost = request.app.state.host
    url = host.settings.sim_control_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.request(method, url, json=json)
    except httpx.HTTPError as exc:
        raise HTTPException(
            503, f"Simulator control is not reachable at {host.settings.sim_control_url}."
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.json().get("detail", resp.text))
    return resp.json()


@router.get("/tools")
async def sim_tools(request: Request):
    return await _call(request, "GET", "/tools")


@router.post("/tools/{tool_id}/{action}")
async def sim_action(tool_id: str, action: str, request: Request, body: Optional[Dict[str, Any]] = None):
    if action not in ACTIONS:
        raise HTTPException(404, f"unknown simulator action {action}")
    host: FabHost = request.app.state.host
    # Log first: a cut link makes the gateway react before the simulator's
    # HTTP response is back, and the activity stream must keep cause before effect.
    body = body or {}
    try:
        host.activity("simulator", tool_id, f"sim.{action}", _describe(action, body), body)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"invalid body for {action}") from exc
    try:
        return await _call(request, "POST", f"/tools/{tool_id}/{ACTIONS[action]}", body)
    except HTTPException as exc:
        host.activity("simulator", tool_id, "sim.failed", f"Simulator rejected {action}: {exc.detail}")
        raise
