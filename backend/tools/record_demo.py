"""Record a real FabLink run for the static demo on GitHub Pages.

Run it against a live system (``make demo``). It records every WebSocket
message the console receives and the REST data the console polls, and it
drives the System map scenarios so each one has a recorded moment the demo can
jump to. The result is replayed by ``frontend/src/replay``.

    cd backend && .venv/bin/python -m tools.record_demo
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import websockets

# Fetched once at the start; afterwards the replay rebuilds them from messages.
SNAPSHOT_PATHS = [
    "/api/equipment",
    "/api/lots?limit=80",
    "/api/secs-log?limit=120",
    "/api/activity?limit=300",
    "/api/modes",
]
# Polled like the console does; stored only when the response changes.
POLLED_PATHS = {
    "/api/alarms?limit=40": 1.0,
    "/api/work-orders?limit=20": 1.0,
    "/api/kpi": 5.0,
    "/api/system": 2.0,
    "/api/sim/tools": 1.0,
}
FRAME_EVERY = 1.5

Pred = Callable[[Dict[str, Any]], bool]


class Recorder:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base, timeout=5)
        self.t0 = 0.0
        self.initial: Dict[str, Any] = {}
        self.rest: Dict[str, List[list]] = {}
        self.ws: List[list] = []
        self.markers: List[Dict[str, Any]] = []

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    async def get(self, path: str) -> Any:
        resp = await self.client.get(path)
        resp.raise_for_status()
        return resp.json()

    # ---- recording ------------------------------------------------------------------------------
    async def snapshot(self) -> List[str]:
        for path in SNAPSHOT_PATHS:
            self.initial[path] = await self.get(path)
        tools = [e["id"] for e in self.initial["/api/equipment"]]
        for tool in tools:
            self.initial[f"/api/equipment/{tool}"] = await self.get(f"/api/equipment/{tool}")
        return tools

    async def listen(self) -> None:
        url = self.base.replace("http", "ws", 1) + "/api/ws"
        async with websockets.connect(url, max_size=None) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                self.ws.append([self.now(), msg["topic"], msg["data"]])

    async def poll(self, path: str, every: float) -> None:
        last = None
        while True:
            try:
                body = await self.get(path)
            except httpx.HTTPError:
                body = None  # e.g. no trace frame yet
            if body is not None:
                text = json.dumps(body, sort_keys=True)
                if text != last:
                    self.rest.setdefault(path, []).append([self.now(), body])
                    last = text
            await asyncio.sleep(every)

    # ---- scenario driving -------------------------------------------------------------------------
    def activity(self, tool: str, since: float) -> List[Dict[str, Any]]:
        return [d for t, topic, d in self.ws if topic == "activity" and d["tool"] == tool and t >= since]

    async def wait_for(self, tool: str, since: float, pred: Pred, timeout: float) -> Optional[float]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for t, topic, d in self.ws:
                if topic == "activity" and t >= since and d["tool"] == tool and pred(d):
                    return t
            await asyncio.sleep(0.2)
        return None

    async def processing_tools(self, exclude: tuple = ()) -> List[str]:
        eq = await self.get("/api/equipment")
        wear = {s["id"]: s["wear"] for s in await self.get("/api/sim/tools")}
        ok = [
            e["id"]
            for e in eq
            if e["connected"]
            and e["state"] == "PROCESSING"
            and not e["work_order"]
            and e["id"] not in exclude
        ]
        return sorted(ok, key=lambda t: wear.get(t, 1.0))

    async def pick(self, exclude: tuple = (), lowest_wear: bool = False) -> str:
        for _ in range(60):
            tools = await self.processing_tools(exclude)
            if tools:
                return tools[0] if lowest_wear else tools[-1]
            await asyncio.sleep(1)
        raise RuntimeError("no processing tool available")

    async def scenario(
        self, sid: str, tool: str, action: Optional[str], body: dict, done: Pred, timeout: float
    ) -> None:
        start = self.now()
        print(f"[{start:6.1f}s] {sid} on {tool}", flush=True)
        if action:
            resp = await self.client.post(f"/api/sim/tools/{tool}/{action}", json=body)
            resp.raise_for_status()
        finished = await self.wait_for(tool, start, done, timeout)
        self.markers.append(
            {"scenario": sid, "tool": tool, "t": start, "end": finished, "complete": bool(finished)}
        )
        print(f"         {'done' if finished else 'DID NOT COMPLETE'} at {finished}", flush=True)

    async def wafer(self, tool: str) -> None:
        since = self.now()
        print(f"[{since:6.1f}s] wafer on {tool}", flush=True)
        started = await self.wait_for(tool, since, lambda d: d["kind"] == "wafer.started", 30)
        if started is None:
            raise RuntimeError("no wafer started")
        pushed = await self.wait_for(tool, started, lambda d: d["kind"] == "ws.pushed", 30)
        self.markers.append(
            {
                "scenario": "wafer",
                "tool": tool,
                "t": round(started - 0.3, 3),
                "end": pushed,
                "complete": bool(pushed),
            }
        )

    async def drive(self) -> None:
        kind = lambda k: lambda d: d["kind"] == k  # noqa: E731
        await asyncio.sleep(4)
        await self.wafer(await self.pick())
        await asyncio.sleep(2)
        await self.scenario(
            "network", await self.pick(), "disconnect", {"seconds": 8}, kind("spool.replayed"), 45
        )
        await asyncio.sleep(3)
        await self.scenario("reply", await self.pick(), "lose_reply", {}, kind("event.duplicate"), 20)
        await asyncio.sleep(3)
        fault_tool = await self.pick()
        await self.scenario(
            "fault",
            fault_tool,
            "wear",
            {"wear": 0.995},
            lambda d: (
                d["kind"] == "state"
                and d["data"].get("state") == "IDLE"
                and "REPAIR" in d["data"].get("reason", "")
            ),
            90,
        )
        await asyncio.sleep(3)
        await self.scenario(
            "drift",
            await self.pick(exclude=(fault_tool,), lowest_wear=True),
            "degrade",
            {"multiplier": 3},
            lambda d: d["kind"] == "maint.completed" and d["data"].get("kind") == "PM",
            150,
        )
        await asyncio.sleep(5)

    async def run(self, out: Path) -> None:
        # Start from a known mode: predictive maintenance, simulation running.
        await self.client.put("/api/modes", json={"predictive": True, "running": True})
        tools = await self.snapshot()
        self.t0 = time.monotonic()
        tasks = [asyncio.ensure_future(self.listen())]
        tasks += [asyncio.ensure_future(self.poll(p, s)) for p, s in POLLED_PATHS.items()]
        tasks += [asyncio.ensure_future(self.poll(f"/api/equipment/{t}/frame", FRAME_EVERY)) for t in tools]
        try:
            await self.drive()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.client.aclose()
        recording = {
            "version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration": self.now(),
            "initial": self.initial,
            "rest": self.rest,
            "ws": self.ws,
            "markers": self.markers,
        }
        raw = json.dumps(recording, separators=(",", ":")).encode()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(gzip.compress(raw, compresslevel=9))
        print(
            f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB, {len(raw) / 1e6:.1f} MB raw): "
            f"{self.now():.0f} s, {len(self.ws)} messages, "
            f"{sum(m['complete'] for m in self.markers)}/{len(self.markers)} scenarios complete"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parents[2] / "frontend/demo/recording.json.gz"
    )
    args = p.parse_args()
    asyncio.run(Recorder(args.base).run(args.out))


if __name__ == "__main__":
    main()
