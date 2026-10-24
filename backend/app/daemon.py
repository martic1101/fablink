"""The fab daemon: simulator, technicians and EAP host in one long-running process.

This is what runs as the Vercel Daemon ``fab`` (and the ``fab`` container in the
split Docker Compose profile). The HSMS links between host and tools stay
inside the process (localhost TCP), exactly as in ``make demo``; what changes
is how the outside world reaches it:

* events and snapshots go out through Redis (see app/redisbus.py);
* console commands come in through Redis and run through app/commands.py;
* when no console is open for ``idle_pause_seconds`` the simulation pauses,
  so an idle fab uses almost no CPU, database or Redis quota, and the first
  viewer wakes it up.

    python -m app.daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from simulator.clock import SimClock
from simulator.control import DegradeIn, DisconnectIn, WearIn
from simulator.crew import MaintenanceCrew
from simulator.tool import SimTool, default_fleet

from . import commands
from .commands import CommandError
from .config import Settings
from .db import create_schema, make_engine, make_sessionmaker
from .host import FabHost
from .models import Alarm, EventLog, Lot, LotStatus, Maintenance, StateHistory, WaferResult, WorkOrderStatus
from .redisbus import COMMANDS, LIVE, LOGS, RedisBus, dumps, reply_channel
from .simulation import SimulatorError

log = logging.getLogger("fab.daemon")

VIEWER_TIMEOUT = 75.0  # a console that hasn't sent a heartbeat for this long is gone
RESUBSCRIBE_EVERY = 600.0  # refresh the command subscription in case the link went quiet


class InProcessSimulator:
    """The simulator's controls without HTTP: it lives in this process."""

    BODIES = {"wear": WearIn, "degrade": DegradeIn, "disconnect": DisconnectIn}

    def __init__(self, tools: Dict[str, SimTool], clock: SimClock) -> None:
        self.tool_map = tools
        self.clock = clock

    async def pause(self) -> None:
        self.clock.pause()

    async def resume(self) -> None:
        self.clock.resume()

    async def is_paused(self) -> Optional[bool]:
        return self.clock.paused

    async def shutdown(self) -> None:
        raise SimulatorError(403, "The cloud fab can't be shut down from the console.")

    async def tools(self) -> List[Dict[str, Any]]:
        return [t.status() for t in self.tool_map.values()]

    async def action(self, tool: str, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        t = self.tool_map.get(tool)
        if t is None:
            raise SimulatorError(404, f"unknown tool {tool}")
        try:
            if action == "wear":
                t.wear = WearIn(**body).wear
            elif action == "degrade":
                t.wear_multiplier = DegradeIn(**body).multiplier
            elif action == "disconnect":
                await t.disconnect(DisconnectIn(**body).seconds)
            elif action == "lose_reply":
                t.lose_next_reply = True
            else:
                raise SimulatorError(404, f"unknown simulator action {action}")
        except ValidationError as exc:
            raise SimulatorError(422, str(exc.errors()[0]["msg"])) from exc
        return t.status()


class InProcessCrewSink:
    """Hands work orders straight to the simulated technicians."""

    def __init__(self, crew: MaintenanceCrew) -> None:
        self.crew = crew

    async def send(self, work_order: Dict[str, Any]) -> None:
        self.crew.accept(work_order["id"], work_order["tool"], work_order["kind"])

    async def cancel(self, work_order_id: int) -> None:
        self.crew.cancel(work_order_id)


class FabDaemon:
    def __init__(
        self, settings: Settings, redis: Any, sessionmaker: async_sessionmaker, base_port: int = 5001
    ):
        self.settings = settings
        self.redis = redis
        self.sessionmaker = sessionmaker
        self.base_port = base_port
        self.viewers: Dict[str, float] = {}
        self.empty_since: Optional[float] = time.monotonic()
        self.dirty = True
        self._batches_at_snapshot = -1  # bus batches already reflected in the snapshot
        self._stopping = False
        self._tasks: List[asyncio.Task] = []
        self.clock = SimClock()
        self.tools: Dict[str, SimTool] = {}
        self.crew: Optional[MaintenanceCrew] = None
        self.host: Optional[FabHost] = None
        self.bus = RedisBus(redis)

    # ---- lifecycle ------------------------------------------------------------------------------
    async def start(self) -> None:
        speed = self.settings.sim_speed
        for spec, wear in zip(default_fleet(self.base_port), (0.55, 0.2, 0.4, 0.1)):
            if self.base_port == 0:
                spec.port = 0  # let the OS pick (tests)
            tool = SimTool(spec, speed=speed, initial_wear=wear, clock=self.clock)
            await tool.serve("127.0.0.1")
            self.tools[spec.tool_id] = tool
        self.crew = MaintenanceCrew(self.tools, travel_seconds=24.0, speed=speed, clock=self.clock)
        self.settings.equipment = [(tid, "127.0.0.1", t.spec.port) for tid, t in self.tools.items()]
        idle_mode = self.settings.idle_pause_seconds > 0
        if idle_mode:
            self.clock.pause()  # nobody is watching yet
        self.host = FabHost(
            self.settings,
            self.sessionmaker,
            self.bus,
            work_order_sink=InProcessCrewSink(self.crew),
            simulator=InProcessSimulator(self.tools, self.clock),
        )
        await self.host.start()
        if idle_mode:
            self.host.paused_by = "idle"
        self._tasks = [
            asyncio.ensure_future(self.bus.run()),
            asyncio.ensure_future(self._command_loop()),
            asyncio.ensure_future(self._snapshot_loop()),
            asyncio.ensure_future(self._idle_loop()),
            asyncio.ensure_future(self._prune_loop()),
        ]
        log.info(
            "fab daemon started: %d tools, speed %g, idle pause %gs",
            len(self.tools),
            speed,
            self.settings.idle_pause_seconds,
        )

    async def stop(self) -> None:
        # Loops check the flag as well: some Redis clients swallow a cancellation
        # that lands inside get_message(), so cancelling alone can't stop them.
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        _, stuck = await asyncio.wait(self._tasks, timeout=7)
        if stuck:
            log.warning("%d daemon task(s) did not stop in time", len(stuck))
        if self.host:
            await self.host.stop()
        if self.crew:
            await self.crew.stop()
        for tool in self.tools.values():
            await tool.stop()
        await self.bus.flush()

    # ---- viewers and idle pause -------------------------------------------------------------------
    async def _viewer_seen(self, viewer: str) -> None:
        self.viewers[viewer] = time.monotonic()
        self.empty_since = None
        host = self.host
        if host and not host.running and host.paused_by == "idle":
            await host.set_running(True, why="A console was opened, so the fab woke up.")
            self.dirty = True

    def _drop_stale_viewers(self) -> None:
        now = time.monotonic()
        for v, seen in list(self.viewers.items()):
            if now - seen > VIEWER_TIMEOUT:
                del self.viewers[v]
        if not self.viewers and self.empty_since is None:
            self.empty_since = now

    async def _idle_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(2)
            self._drop_stale_viewers()
            limit = self.settings.idle_pause_seconds
            host = self.host
            if not (limit > 0 and host and host.running and self.empty_since is not None):
                continue
            if time.monotonic() - self.empty_since >= limit:
                await host.set_running(
                    False, why="No console has been open for a while, so the fab is paused to save compute."
                )
                self.dirty = True

    # ---- commands ---------------------------------------------------------------------------------
    async def _command_loop(self) -> None:
        while not self._stopping:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(COMMANDS)
                renew_at = time.monotonic() + RESUBSCRIBE_EVERY
                while time.monotonic() < renew_at and not self._stopping:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                    if msg:
                        asyncio.ensure_future(self.handle(json.loads(msg["data"])))
                    else:
                        # Some clients return at once instead of waiting out the
                        # timeout; never spin without giving the loop a turn.
                        await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("command subscription failed (%s); reconnecting", exc)
                await asyncio.sleep(2)
            finally:
                try:
                    await pubsub.unsubscribe()
                    await pubsub.aclose()
                except Exception:
                    pass

    async def handle(self, msg: Dict[str, Any]) -> None:
        name, args = msg.get("cmd", ""), msg.get("args") or {}
        if name == "viewer":
            await self._viewer_seen(str(args.get("id")))
            return
        if name == "viewer_left":
            self.viewers.pop(str(args.get("id")), None)
            self._drop_stale_viewers()
            return
        try:
            reply: Dict[str, Any] = {"ok": True, "result": await commands.execute(self.host, name, args)}
        except CommandError as exc:
            reply = {"ok": False, "status": exc.status, "detail": exc.detail}
        except Exception:
            log.exception("command %s failed", name)
            reply = {"ok": False, "status": 500, "detail": f"{name} failed inside the fab daemon"}
        self.dirty = True
        if msg.get("id"):
            await self.redis.publish(reply_channel(msg["id"]), dumps(reply))

    # ---- snapshots ----------------------------------------------------------------------------------
    async def write_snapshot(self) -> None:
        host = self.host
        assert host is not None
        live = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "modes": host.modes(),
            "equipment": [host.equipment_view(l) for l in host.links.values()],
            "frames": {tid: host.frame_view(l) for tid, l in host.links.items() if l.last_frame},
            "sim": [t.status() for t in self.tools.values()],
            "viewers": len(self.viewers),
            "secs_count": len(host.secs_log),
        }
        logs = {
            "traces": {tid: host.trace_view(l, 120) for tid, l in host.links.items()},
            "activity": list(host.activity_log)[-200:],
            "secs": [e for e in host.secs_log if e["sf"] not in ("S6F1", "S6F2")][-80:],
        }
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.set(LIVE, dumps(live))
            pipe.set(LOGS, dumps(logs))
            await pipe.execute()
        self.dirty = False
        self._batches_at_snapshot = self.bus.published

    async def _snapshot_loop(self) -> None:
        while not self._stopping:
            # A paused fab rarely changes, so it doesn't spend Redis commands either;
            # but tools still connect and drop while paused, and those events count.
            changed = self.bus.published != self._batches_at_snapshot
            if self.dirty or changed or (self.host and self.host.running):
                try:
                    await self.write_snapshot()
                except Exception as exc:
                    log.warning("snapshot failed: %s", exc)
            await asyncio.sleep(self.settings.snapshot_every)

    # ---- housekeeping ---------------------------------------------------------------------------------
    async def prune(self) -> None:
        """Keep the database small (Neon's free plan has 0.5 GB)."""
        now = datetime.now(timezone.utc)
        async with self.sessionmaker() as s:
            await s.execute(delete(EventLog).where(EventLog.created_at < now - timedelta(hours=6)))
            await s.execute(delete(WaferResult).where(WaferResult.created_at < now - timedelta(hours=48)))
            await s.execute(delete(StateHistory).where(StateHistory.ended_at < now - timedelta(hours=24)))
            await s.execute(
                delete(Lot).where(
                    Lot.status == LotStatus.COMPLETED, Lot.finished_at < now - timedelta(hours=24)
                )
            )
            await s.execute(delete(Alarm).where(Alarm.cleared_at < now - timedelta(hours=24)))
            await s.execute(
                delete(Maintenance).where(
                    Maintenance.status.in_([WorkOrderStatus.DONE, WorkOrderStatus.CANCELLED]),
                    Maintenance.finished_at < now - timedelta(days=7),
                )
            )
            await s.commit()

    async def _prune_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(600)
            if self.host and self.host.running:
                try:
                    await self.prune()
                except Exception as exc:
                    log.warning("pruning failed: %s", exc)


async def main() -> None:
    import redis.asyncio as aioredis

    settings = Settings()
    if not settings.redis_url:
        raise SystemExit("REDIS_URL (or KV_URL) must be set for the fab daemon.")
    # A long-running process can hold a direct connection; prefer it over the pooler.
    engine = make_engine(os.getenv("DATABASE_URL_UNPOOLED") or settings.database_url)
    await create_schema(engine)
    redis = aioredis.from_url(settings.redis_url)
    daemon = FabDaemon(settings, redis, make_sessionmaker(engine))
    await daemon.start()
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("stopping fab daemon")
    await daemon.stop()
    await engine.dispose()
    await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
