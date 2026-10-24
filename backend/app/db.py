from __future__ import annotations

from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    """Accept the URLs hosting providers hand out and make them asyncpg URLs.

    Neon (via the Vercel Marketplace) provides ``postgres://...?sslmode=require
    &channel_binding=require``; SQLAlchemy's asyncpg driver needs
    ``postgresql+asyncpg://`` and understands ``ssl=`` rather than ``sslmode=``.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix) :]
    if not url.startswith("postgresql+asyncpg://"):
        return url
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query):
        if key == "sslmode":
            query.append(("ssl", "require" if value in ("require", "prefer", "allow") else value))
        elif key == "channel_binding":
            continue  # libpq option asyncpg doesn't take
        else:
            query.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def make_engine(url: str, serverless: bool = False) -> AsyncEngine:
    url = normalize_database_url(url)
    kwargs: Dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    elif serverless:
        # A function instance may freeze between requests, so don't keep a pool;
        # and Neon's pooled endpoint is PgBouncer, which doesn't keep prepared
        # statements between transactions.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"statement_cache_size": 0}
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    # A real deployment would use Alembic migrations; create_all keeps the
    # demo zero-setup.
    from . import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
