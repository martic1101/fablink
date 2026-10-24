"""The Redis link between the fab daemon and the stateless cloud API.

What travels over Redis, and why it is shaped this way:

* ``fab:events`` (pub/sub): the daemon's live messages, **batched** every half
  second into one PUBLISH. One message per event would be ~100 Redis commands
  a second, enough to use up a free Upstash plan (500K commands/month) in a day.
* ``fab:live`` / ``fab:logs`` (keys): snapshots of the live state, written
  every few seconds while the fab runs. ``live`` is small (tool views, modes,
  frames) and read often; ``logs`` (trace, activity, SECS log) is bigger and
  only read when a console (re)connects.
* ``fab:commands`` (pub/sub) and ``fab:reply:<id>``: console commands and their
  answers. PUBLISH reports how many subscribers got a message, so the API knows
  immediately when the daemon isn't running. Daemons can't receive inbound
  traffic on Vercel, which rules out plain HTTP calls to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from .bus import EventBus

log = logging.getLogger(__name__)

EVENTS = "fab:events"
COMMANDS = "fab:commands"
LIVE = "fab:live"
LOGS = "fab:logs"


def reply_channel(request_id: str) -> str:
    return f"fab:reply:{request_id}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


class RedisBus(EventBus):
    """An EventBus that also forwards everything to Redis in batches."""

    def __init__(self, redis: Any, interval: float = 0.5, maxsize: int = 1000) -> None:
        super().__init__(maxsize)
        self.redis = redis
        self.interval = interval
        self._pending: List[Dict[str, Any]] = []
        self.published = 0  # batches sent; useful for tests and logs

    def publish(self, topic: str, data: Any) -> None:
        super().publish(topic, data)
        self._pending.append({"topic": topic, "data": data})

    async def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            await self.redis.publish(EVENTS, dumps(batch))
            self.published += 1
        except Exception as exc:  # Redis hiccup: drop this batch, consoles resync on reconnect
            log.warning("could not publish %d events: %s", len(batch), exc)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()
