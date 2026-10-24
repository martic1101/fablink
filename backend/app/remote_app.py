"""The stateless cloud API (Vercel service ``api``).

Same routes as the single-process app, but the fab runs in the ``fab`` daemon:
live state and commands go through Redis, history through Postgres. Nothing is
kept in this process between requests, so Vercel can run as many instances as
it likes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker

from .api.routes import router
from .config import Settings
from .db import make_engine, make_sessionmaker
from .remote import RemoteFab

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_remote_app(
    settings: Optional[Settings] = None,
    redis: Any = None,
    sessionmaker: Optional[async_sessionmaker] = None,
) -> FastAPI:
    """Pass ``redis`` and ``sessionmaker`` to wire the app yourself (tests).

    No lifespan hook: a serverless platform may not run one, and neither the
    engine nor the Redis client connects until the first request uses it.
    """
    settings = settings or Settings()
    app = FastAPI(title="FabLink API (cloud)", version="1.0.0")
    app.include_router(router)
    if redis is not None and sessionmaker is not None:
        app.state.fab = RemoteFab(redis, sessionmaker, snapshot_ttl=0)
    elif settings.redis_url:
        import redis.asyncio as aioredis

        engine = make_engine(settings.database_url, serverless=True)
        app.state.fab = RemoteFab(aioredis.from_url(settings.redis_url), make_sessionmaker(engine))
    else:
        app.state.fab = None  # routes answer 503 and say what is missing
    # Local "split" container only; on Vercel the web service serves the console.
    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app
