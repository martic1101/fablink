"""Talking to the simulated fab (demo only).

The host uses this for the console's simulation controls: pausing simulated
time, scenario actions on a tool, listing the simulator's ground truth and
shutting it down. ``SimulatorControl`` reaches a separate simulator process
over HTTP (local ``make demo``); the cloud daemon runs the simulator in the
same process and plugs in an in-process implementation with the same methods.
A real deployment has no simulator and leaves this unset.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

import httpx

# Scenario actions the console can trigger, mapped to the control API's paths.
ACTIONS: Dict[str, str] = {
    "wear": "wear",
    "degrade": "degrade",
    "disconnect": "disconnect",
    "lose_reply": "lose-reply",
}


class SimulatorError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Simulator(Protocol):
    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def is_paused(self) -> Optional[bool]: ...

    async def shutdown(self) -> None: ...

    async def tools(self) -> List[Dict[str, Any]]: ...

    async def action(self, tool: str, action: str, body: Dict[str, Any]) -> Dict[str, Any]: ...


class SimulatorControl:
    """The simulator's control API over HTTP."""

    def __init__(
        self, base_url: str, timeout: float = 3.0, transport: Optional[httpx.AsyncBaseTransport] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport  # tests plug in an in-process ASGI app

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                resp = await client.request(method, f"{self.base_url}{path}", json=json)
        except httpx.HTTPError as exc:
            raise SimulatorError(503, f"Simulator control is not reachable at {self.base_url}.") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise SimulatorError(resp.status_code, str(detail))
        return resp.json()

    async def pause(self) -> None:
        await self._request("POST", "/clock/pause")

    async def resume(self) -> None:
        await self._request("POST", "/clock/resume")

    async def is_paused(self) -> Optional[bool]:
        return bool((await self._request("GET", "/clock"))["paused"])

    async def shutdown(self) -> None:
        await self._request("POST", "/shutdown")

    async def tools(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/tools")

    async def action(self, tool: str, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if action not in ACTIONS:
            raise SimulatorError(404, f"unknown simulator action {action}")
        return await self._request("POST", f"/tools/{tool}/{ACTIONS[action]}", body)
