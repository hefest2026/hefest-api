from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI
from pydantic import BaseModel
from tortoise.contrib.fastapi import RegisterTortoise

from hefest.config import TORTOISE_ORM, settings


async def _migrate() -> None:
    """Run pending Tortoise migrations before accepting traffic."""
    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "tortoise", "-c", "hefest.config.TORTOISE_ORM", "migrate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Migrations failed:\n{stderr.decode()}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run migrations, then initialise Tortoise ORM and Redis."""
    await _migrate()
    async with RegisterTortoise(
        app,
        config=TORTOISE_ORM,
        generate_schemas=False,
    ):
        app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            yield
        finally:
            await app.state.redis.aclose()


app = FastAPI(
    title="Hefest API",
    description="School Events & Notification Center — AIBEST 2026 Burgas",
    version="0.1.0",
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    """Liveness probe response."""

    status: str
    version: str


class ReadyResponse(BaseModel):
    """Readiness probe response."""

    status: str
    postgres: str
    redis: str


@app.get("/health", response_model=HealthResponse, tags=["operational"])
async def health() -> HealthResponse:
    """Liveness probe — returns 200 unconditionally."""
    return HealthResponse(status="ok", version=app.version)


@app.get("/ready", response_model=ReadyResponse, tags=["operational"])
async def ready() -> ReadyResponse:
    """Readiness probe — checks Postgres and Redis connectivity."""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await conn.execute_query("SELECT 1")
    await app.state.redis.ping()
    return ReadyResponse(status="ok", postgres="ok", redis="ok")
