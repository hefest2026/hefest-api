from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from hefest.redis import RATELIMIT_PREFIX

router = APIRouter(prefix="/internal", tags=["internal"])


@router.delete("/flush-ratelimit", status_code=status.HTTP_200_OK)
async def flush_ratelimit(request: Request) -> JSONResponse:
    """Delete all rate-limit keys for the calling IP.

    Only reachable when HEFEST_ENV != 'production'. Guarded in main.py.
    """
    from hefest.middleware.rate_limit import _client_ip

    ip = _client_ip(request)
    redis = request.app.state.redis
    pattern = f"{RATELIMIT_PREFIX}:*:ip:{ip}"
    keys = await redis.keys(pattern)
    if keys:
        await redis.delete(*keys)
    return JSONResponse({"flushed": len(keys), "ip": ip})
