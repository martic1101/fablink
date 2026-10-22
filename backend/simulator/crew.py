"""Simulated maintenance technicians.

In a fab, the host never "starts a PM" over SECS. It raises a work order and
stops sending lots to the tool; a technician walks to the tool, switches it to
ONLINE LOCAL and does the work there, and the tool reports what happened.

This crew plays the technicians. The host hands it work orders through its
work-order integration (in the demo, an HTTP call to the simulator's control
API; in production, a CMMS or paging system).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .clock import SimClock
from .tool import SimTool

log = logging.getLogger("simulator.crew")


@dataclass
class Job:
    work_order: int
    tool: str
    kind: str  # PM | REPAIR
    status: str = "TRAVELLING"  # TRAVELLING, WAITING_FOR_TOOL, WORKING, DONE, CANCELLED
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def view(self) -> Dict[str, object]:
        return {"work_order": self.work_order, "tool": self.tool, "kind": self.kind, "status": self.status}


class MaintenanceCrew:
    def __init__(
        self,
        tools: Dict[str, SimTool],
        travel_seconds: float = 24.0,
        speed: float = 1.0,
        clock: Optional[SimClock] = None,
    ) -> None:
        self.tools = tools
        self.travel_seconds = travel_seconds  # simulated seconds, scaled by speed
        self.speed = speed
        self.clock = clock or SimClock()
        self.jobs: Dict[int, Job] = {}

    def accept(self, work_order: int, tool: str, kind: str) -> Job:
        """Idempotent: the host may resend a work order it is unsure about."""
        if tool not in self.tools:
            raise KeyError(tool)
        if kind not in ("PM", "REPAIR"):
            raise ValueError(kind)
        job = self.jobs.get(work_order)
        if job is None:
            job = Job(work_order, tool, kind)
            job.task = asyncio.ensure_future(self._work(job))
            self.jobs[work_order] = job
        return job

    def cancel(self, work_order: int) -> Optional[Job]:
        job = self.jobs.get(work_order)
        if job and job.status in ("TRAVELLING", "WAITING_FOR_TOOL") and job.task:
            job.task.cancel()
            job.status = "CANCELLED"
        return job

    def status(self) -> List[Dict[str, object]]:
        return [j.view() for j in self.jobs.values() if j.status not in ("DONE", "CANCELLED")]

    async def _work(self, job: Job) -> None:
        tool = self.tools[job.tool]
        await self.clock.sleep(self.travel_seconds / self.speed)
        job.status = "WAITING_FOR_TOOL"
        # A PM waits for the current lot to finish (the host has stopped
        # dispatching to this tool); a repair needs the tool to be DOWN.
        while not tool.can_maintain(job.kind):
            await asyncio.sleep(0.1)
        job.status = "WORKING"
        log.info("technician on %s: %s for work order %d", job.tool, job.kind, job.work_order)
        await tool.local_maintenance(job.kind, job.work_order)
        job.status = "DONE"

    async def stop(self) -> None:
        for job in self.jobs.values():
            if job.task:
                job.task.cancel()
