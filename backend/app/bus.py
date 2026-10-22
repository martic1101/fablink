"""In-process pub/sub that feeds WebSocket clients.

Each subscriber gets a bounded queue; a slow browser tab drops messages
instead of growing memory or blocking the equipment message path.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Set


class EventBus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._subs: Set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, topic: str, data: Any) -> None:
        msg: Dict[str, Any] = {"topic": topic, "data": data}
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
