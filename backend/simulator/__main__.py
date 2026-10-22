"""Run the simulated tool fleet:  python -m simulator [--speed 2]"""

from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from .clock import SimClock
from .control import create_control_app
from .crew import MaintenanceCrew
from .tool import SimTool, default_fleet

log = logging.getLogger("simulator")


async def main(
    speed: float, base_port: int, host: str, control_host: str, control_port: int, travel: float
) -> None:
    clock = SimClock()  # one clock for the whole fab, so a pause freezes everything
    tools = [
        SimTool(spec, speed=speed, initial_wear=w, clock=clock)
        for spec, w in zip(default_fleet(base_port), (0.55, 0.2, 0.4, 0.1))
    ]
    for t in tools:
        await t.serve(host)
    by_id = {t.spec.tool_id: t for t in tools}
    crew = MaintenanceCrew(by_id, travel_seconds=travel, speed=speed, clock=clock)

    def request_exit() -> None:
        log.info("shutdown requested")
        server.should_exit = True

    # Control API (scenarios, clock, technicians' inbox) in the same event loop.
    app = create_control_app(by_id, crew, clock, on_shutdown=request_exit)
    server = uvicorn.Server(uvicorn.Config(app, host=control_host, port=control_port, log_level="warning"))
    log.info("control API on http://%s:%d", control_host, control_port)
    await server.serve()
    await crew.stop()
    for t in tools:
        await t.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Simulated fab equipment (HSMS passive)")
    p.add_argument("--speed", type=float, default=1.0, help="time acceleration factor")
    p.add_argument("--base-port", type=int, default=5001)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--control-host", default="127.0.0.1")
    p.add_argument("--control-port", type=int, default=8100)
    p.add_argument("--crew-travel", type=float, default=24.0, help="technician travel time (simulated s)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        a = args
        asyncio.run(main(a.speed, a.base_port, a.host, a.control_host, a.control_port, a.crew_travel))
    except KeyboardInterrupt:
        pass
