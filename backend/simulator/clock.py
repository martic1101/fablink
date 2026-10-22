"""Simulated time that can be paused.

Only *simulated* activity waits on this clock: wafers, maintenance, technician
travel, trace sampling. Real network behaviour (HSMS sessions, linktest, T3)
keeps running, so pausing the simulation never drops a connection.
"""

from __future__ import annotations

import asyncio
from typing import Optional

STEP = 0.25  # seconds; how quickly a pause takes effect inside a long sleep


class SimClock:
    def __init__(self) -> None:
        self.paused = False
        self._resumed: Optional[asyncio.Event] = None

    async def sleep(self, seconds: float) -> None:
        """Like asyncio.sleep, but time only passes while not paused."""
        remaining = seconds
        while remaining > 0:
            if self.paused:
                if self._resumed is None:
                    self._resumed = asyncio.Event()
                await self._resumed.wait()
                continue
            step = min(remaining, STEP)
            await asyncio.sleep(step)
            remaining -= step

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        if self._resumed is not None:
            self._resumed.set()
            self._resumed = None
