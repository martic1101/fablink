"""ASGI entrypoint:  uvicorn app.main:app"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .bus import EventBus
from .config import Settings
from .db import create_schema, make_engine, make_sessionmaker
from .fab import LocalFab
from .host import FabHost
from .maintenance import CrewHttpSink
from .simulation import SimulatorControl

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        await create_schema(engine)
        # Work orders go to the simulated technicians; in production this would be
        # the fab's maintenance system.
        sink = CrewHttpSink(settings.sim_control_url)
        host = FabHost(
            settings,
            make_sessionmaker(engine),
            EventBus(),
            work_order_sink=sink,
            simulator=SimulatorControl(settings.sim_control_url),
        )
        app.state.host = host
        app.state.fab = LocalFab(host)
        await host.start()
        try:
            yield
        finally:
            await host.stop()
            await engine.dispose()

    app = FastAPI(title="FabLink EAP", version="1.0.0", lifespan=lifespan)

    def request_exit() -> None:
        # Let the HTTP response go out first, then stop like Ctrl+C would.
        asyncio.get_event_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGINT)

    app.state.request_exit = request_exit
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"]
    )
    app.include_router(router)
    if FRONTEND_DIST.exists():  # production build served by the same process
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
