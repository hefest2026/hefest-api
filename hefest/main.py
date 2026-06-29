from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from typing import Final

import redis.asyncio as aioredis
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel
from tortoise.contrib.fastapi import RegisterTortoise
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from hefest.config import TORTOISE_ORM, settings
from hefest.logging import configure_logging
from hefest.middleware.rate_limit import SLIDING_WINDOW_LUA, RateLimitMiddleware
from hefest.routers.auth import router as auth_router
from hefest.routers.device import router as device_router
from hefest.routers.events import router as events_router
from hefest.routers.internal import router as internal_router
from hefest.routers.notification_jobs import router as notification_jobs_router
from hefest.routers.registrations import router as registrations_router
from hefest.routers.sso import router as sso_router

configure_logging(settings)

READY_CHECK_TIMEOUT: Final = 2.0
"""Per-dependency readiness check timeout in seconds."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialise Tortoise ORM and Redis on startup; close on shutdown."""
    async with RegisterTortoise(
        app,
        config=TORTOISE_ORM,
        generate_schemas=False,
    ):
        app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.rate_limit_script = app.state.redis.register_script(
            SLIDING_WINDOW_LUA
        )
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)
# ProxyHeadersMiddleware runs first (reverse registration order): it rewrites
# request.client.host from X-Forwarded-For only when the direct peer is in
# trusted_proxies, preventing clients from spoofing their IP.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)

app.include_router(auth_router)
app.include_router(sso_router)
app.include_router(events_router)
app.include_router(registrations_router)
app.include_router(notification_jobs_router)
app.include_router(device_router)
if settings.env != "production":
    app.include_router(internal_router)


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


async def _check_dependency(name: str, probe: Awaitable[object]) -> str:
    """Run a readiness probe, returning ``"ok"`` or ``"down"``.

    Any failure (including timeout) is reported as ``"down"`` and logged;
    a readiness probe must never raise, so the per-dependency status can be
    surfaced in the response body.

    Args:
        name: Dependency label used for logging.
        probe: Awaitable that resolves when the dependency is reachable.

    Returns:
        ``"ok"`` if the probe succeeded within the timeout, else ``"down"``.
    """
    try:
        await asyncio.wait_for(probe, timeout=READY_CHECK_TIMEOUT)
        return "ok"
    except Exception:
        logger.opt(exception=True).warning("Readiness check failed for {}", name)
        return "down"


@app.get(
    "/ready",
    response_model=ReadyResponse,
    tags=["operational"],
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(response: Response) -> ReadyResponse:
    """Readiness probe — 200 if all deps reachable, 503 otherwise."""
    from tortoise import Tortoise

    postgres, redis = await asyncio.gather(
        _check_dependency(
            "postgres", Tortoise.get_connection("default").execute_query("SELECT 1")
        ),
        _check_dependency("redis", app.state.redis.ping()),
    )

    ready_ok = postgres == "ok" and redis == "ok"
    if not ready_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ok" if ready_ok else "degraded",
        postgres=postgres,
        redis=redis,
    )
