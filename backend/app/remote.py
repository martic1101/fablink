"""RemoteFab: the cloud API's view of a fab that runs in a separate daemon.

Stateless on purpose: any number of API instances (and WebSocket connections
pinned to different instances) see the same fab, because live state comes from
Redis snapshots and events written by the one daemon, history comes from
Postgres, and commands are forwarded to the daemon over Redis.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .commands import CommandError
from .fab import Fab
from .redisbus import COMMANDS, EVENTS, LIVE, LOGS, dumps, reply_channel

SNAPSHOT_TTL = 2.0  # seconds an API instance reuses a snapshot it already read
COMMAND_TIMEOUT = 10.0
HEARTBEAT_EVERY = 30.0  # a connected console tells the daemon it is still there


class RemoteFab(Fab):
    capabilities = {"shutdown": False}

    def __init__(self, redis: Any, sessionmaker: async_sessionmaker, snapshot_ttl: float = SNAPSHOT_TTL):
        self.redis = redis
        self._sessionmaker = sessionmaker
        self.snapshot_ttl = snapshot_ttl
        self._cache: Dict[str, Tuple[float, Any]] = {}

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as s:
            yield s

    # ---- snapshots ------------------------------------------------------------------------------
    async def _snapshot(self, key: str, fresh: bool = False) -> Dict[str, Any]:
        cached = self._cache.get(key)
        if cached and not fresh and time.monotonic() - cached[0] < self.snapshot_ttl:
            return cached[1]
        raw = await self.redis.get(key)
        data = json.loads(raw) if raw else {}
        self._cache[key] = (time.monotonic(), data)
        return data

    async def status(self) -> Dict[str, Any]:
        live = await self._snapshot(LIVE)
        tools = live.get("equipment", [])
        return {
            "equipment_online": sum(1 for t in tools if t.get("connected")),
            "equipment_total": len(tools),
            "ws_clients": live.get("viewers", 0),
            "secs_messages_logged": live.get("secs_count", 0),
        }

    async def equipment(self) -> List[Dict[str, Any]]:
        return (await self._snapshot(LIVE)).get("equipment", [])

    async def equipment_detail(self, tool: str) -> Optional[Dict[str, Any]]:
        view = next((t for t in await self.equipment() if t["id"] == tool), None)
        if view is None:
            return None
        trace = (await self._snapshot(LOGS)).get("traces", {}).get(tool, [])
        return {**view, "trace": trace}

    async def frame(self, tool: str) -> Optional[Dict[str, Any]]:
        return (await self._snapshot(LIVE)).get("frames", {}).get(tool)

    async def activity(self) -> List[Dict[str, Any]]:
        return (await self._snapshot(LOGS)).get("activity", [])

    async def secs_log(self) -> List[Dict[str, Any]]:
        return (await self._snapshot(LOGS)).get("secs", [])

    async def modes(self) -> Dict[str, Any]:
        return (await self._snapshot(LIVE)).get("modes", {})

    # ---- commands -------------------------------------------------------------------------------
    async def notify(self, name: str, **args: Any) -> int:
        """Fire-and-forget message to the daemon; returns how many daemons heard it."""
        return await self.redis.publish(COMMANDS, dumps({"cmd": name, "args": args}))

    async def command(self, name: str, **args: Any) -> Any:
        if name == "sim_tools":  # read-only: answered from the snapshot, no round trip
            return (await self._snapshot(LIVE)).get("sim", [])
        request_id = uuid.uuid4().hex
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(reply_channel(request_id))
        try:
            heard = await self.redis.publish(COMMANDS, dumps({"id": request_id, "cmd": name, "args": args}))
            if not heard:
                raise CommandError(503, "The fab daemon is not running, so nothing can be changed right now.")
            deadline = time.monotonic() + COMMAND_TIMEOUT
            while time.monotonic() < deadline:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    await asyncio.sleep(0.02)  # don't spin if the client returned at once
                    continue
                reply = json.loads(msg["data"])
                if reply.get("ok"):
                    self._cache.clear()  # the change should show on the next read
                    return reply.get("result")
                raise CommandError(reply.get("status", 500), reply.get("detail", "command failed"))
            raise CommandError(504, "The fab daemon did not answer in time.")
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    # ---- live stream ----------------------------------------------------------------------------
    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        viewer = uuid.uuid4().hex
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(EVENTS)  # before the greeting, so nothing falls in between
        try:
            await self.notify("viewer", id=viewer)  # wakes a fab paused for lack of viewers
            live = await self._snapshot(LIVE, fresh=True)
            yield {"topic": "modes", "data": live.get("modes", {})}
            for view in live.get("equipment", []):
                yield {"topic": "equipment", "data": view}
            next_beat = time.monotonic() + HEARTBEAT_EVERY
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if time.monotonic() >= next_beat:
                    await self.notify("viewer", id=viewer)
                    next_beat = time.monotonic() + HEARTBEAT_EVERY
                if msg is None:
                    await asyncio.sleep(0.02)  # don't spin if the client returned at once
                    continue
                for event in json.loads(msg["data"]):
                    yield event
        finally:
            try:
                await self.notify("viewer_left", id=viewer)
            except Exception:
                pass
            await pubsub.unsubscribe()
            await pubsub.aclose()
