"""What the API needs from the fab, behind one interface.

``LocalFab`` wraps a FabHost running in the same process (``make demo``,
Docker). ``RemoteFab`` (app/remote.py) is the cloud version: the fab runs in
a separate daemon, live state comes from Redis snapshots and events, history
from Postgres, and commands travel over Redis. The routes in app/api only see
this interface, so both deployments serve exactly the same API.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from . import commands
from .host import FabHost


class Fab:
    """Interface implemented by LocalFab and RemoteFab."""

    capabilities: Dict[str, bool] = {"shutdown": False}

    def session(self) -> Any:  # async context manager yielding AsyncSession
        raise NotImplementedError

    async def status(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def equipment(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def equipment_detail(self, tool: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def frame(self, tool: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def activity(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def secs_log(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def modes(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def command(self, name: str, **args: Any) -> Any:
        """Run a command; raises commands.CommandError."""
        raise NotImplementedError

    def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Live messages for one WebSocket client: a greeting, then events."""
        raise NotImplementedError


class LocalFab(Fab):
    def __init__(self, host: FabHost) -> None:
        self.host = host

    @property
    def capabilities(self) -> Dict[str, bool]:  # type: ignore[override]
        return {"shutdown": self.host.settings.allow_shutdown}

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.host.session() as s:
            yield s

    async def status(self) -> Dict[str, Any]:
        links = self.host.links.values()
        return {
            "equipment_online": sum(1 for l in links if l.connected),
            "equipment_total": len(self.host.links),
            "ws_clients": self.host.bus.subscriber_count,
            "secs_messages_logged": len(self.host.secs_log),
        }

    async def equipment(self) -> List[Dict[str, Any]]:
        return [self.host.equipment_view(l) for l in self.host.links.values()]

    async def equipment_detail(self, tool: str) -> Optional[Dict[str, Any]]:
        link = self.host.links.get(tool)
        if link is None:
            return None
        return {**self.host.equipment_view(link), "trace": self.host.trace_view(link)}

    async def frame(self, tool: str) -> Optional[Dict[str, Any]]:
        link = self.host.links.get(tool)
        return self.host.frame_view(link) if link else None

    async def activity(self) -> List[Dict[str, Any]]:
        return list(self.host.activity_log)

    async def secs_log(self) -> List[Dict[str, Any]]:
        return list(self.host.secs_log)

    async def modes(self) -> Dict[str, Any]:
        return self.host.modes()

    async def command(self, name: str, **args: Any) -> Any:
        return await commands.execute(self.host, name, args)

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        q = self.host.bus.subscribe()
        try:
            yield {"topic": "modes", "data": self.host.modes()}
            for link in self.host.links.values():
                yield {"topic": "equipment", "data": self.host.equipment_view(link)}
            while True:
                yield await q.get()
        finally:
            self.host.bus.unsubscribe(q)
