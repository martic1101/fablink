"""Client for the simulator's control API (demo only).

Used for the console's simulation controls: pausing simulated time and shutting
the simulator down. A real deployment has no simulator and leaves this unset.
"""

from __future__ import annotations

from typing import Optional

import httpx


class SimulatorControl:
    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _request(self, method: str, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, f"{self.base_url}{path}")
            resp.raise_for_status()
            return resp.json()

    async def pause(self) -> None:
        await self._request("POST", "/clock/pause")

    async def resume(self) -> None:
        await self._request("POST", "/clock/resume")

    async def is_paused(self) -> Optional[bool]:
        return bool((await self._request("GET", "/clock"))["paused"])

    async def shutdown(self) -> None:
        await self._request("POST", "/shutdown")
