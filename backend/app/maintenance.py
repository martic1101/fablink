"""Where the host's maintenance work orders go.

A real host does not perform maintenance; it raises a work order in the fab's
maintenance system (CMMS) or pages the technicians, and stops dispatching lots
to the tool. ``WorkOrderSink`` is that integration point. The demo uses
``CrewHttpSink``, which hands work orders to the simulated technician crew in
the simulator process.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol

import httpx


class WorkOrderSink(Protocol):
    async def send(self, work_order: Dict[str, Any]) -> None: ...

    async def cancel(self, work_order_id: int) -> None: ...


class CrewHttpSink:
    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def send(self, work_order: Dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/crew/work-orders",
                json={"id": work_order["id"], "tool": work_order["tool"], "kind": work_order["kind"]},
            )
            resp.raise_for_status()

    async def cancel(self, work_order_id: int) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/crew/work-orders/{work_order_id}/cancel")
            if resp.status_code != 404:
                resp.raise_for_status()
