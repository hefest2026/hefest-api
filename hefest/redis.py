from __future__ import annotations

from typing import Final

import redis.asyncio as aioredis
from fastapi import Request

STREAM_NAME: Final = "hefest:notifications"
RATELIMIT_PREFIX: Final = "hefest:ratelimit"


def get_redis(request: Request) -> aioredis.Redis:
    """FastAPI dependency — shared Redis client from app state."""
    return request.app.state.redis
