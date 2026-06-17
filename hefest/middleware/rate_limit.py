from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import jwt
from fastapi import status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from hefest.config import settings
from hefest.redis import RATELIMIT_PREFIX

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from redis.commands.core import AsyncScript

_EVENTS_REGISTER_RE: Final = re.compile(r"^/events/[^/]+/registrations$")

# Atomic sliding-window check. Runs inside Redis so the window size is evaluated
# *before* deciding whether to record the request — a blocked request never adds
# to the set, which prevents a spam loop from keeping the window permanently full
# (indefinite lockout). The unique member also avoids same-timestamp collisions
# that would otherwise undercount concurrent requests. Returns {is_limited,
# retry_after_seconds}.
SLIDING_WINDOW_LUA: Final = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.ceil(window))
    return {0, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_after = math.ceil(window)
if oldest[2] then
    retry_after = math.max(1, math.ceil(window - (now - tonumber(oldest[2]))))
end
return {1, retry_after}
"""


@dataclass(frozen=True)
class _Rule:
    methods: frozenset[str]
    path_re: re.Pattern[str] | None
    path_exact: str | None
    identifier: Literal["ip", "user"]
    tag: str
    limit: int
    window: int

    def matches(self, method: str, path: str) -> bool:
        if method not in self.methods:
            return False
        if self.path_exact is not None:
            return path == self.path_exact
        if self.path_re is not None:
            return bool(self.path_re.match(path))
        return False


_RULES: Final[list[_Rule]] = [
    _Rule(
        methods=frozenset({"POST"}),
        path_re=None,
        path_exact="/login",
        identifier="ip",
        tag="login",
        limit=settings.rate_limit_login_count,
        window=settings.rate_limit_login_window,
    ),
    _Rule(
        methods=frozenset({"POST"}),
        path_re=None,
        path_exact="/register",
        identifier="ip",
        tag="register",
        limit=settings.rate_limit_register_count,
        window=settings.rate_limit_register_window,
    ),
    _Rule(
        methods=frozenset({"POST"}),
        path_re=_EVENTS_REGISTER_RE,
        path_exact=None,
        identifier="user",
        tag="event_register",
        limit=settings.rate_limit_event_register_count,
        window=settings.rate_limit_event_register_window,
    ),
]

_GLOBAL_RULE: Final = _Rule(
    methods=frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}),
    path_re=None,
    path_exact=None,
    identifier="ip",
    tag="global",
    limit=settings.rate_limit_global_count,
    window=settings.rate_limit_global_window,
)


def _client_ip(request: Request) -> str:
    # ProxyHeadersMiddleware (mounted in main.py) has already resolved
    # X-Forwarded-For against the trusted-proxy list and rewritten
    # request.client.host to the real client IP, so we never read the
    # raw header here — doing so would let any client spoof their IP.
    return request.client.host if request.client else "unknown"


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _verified_user_id(token: str | None) -> str | None:
    """Return the `sub` of a *cryptographically verified* JWT, else ``None``.

    The signature MUST be checked before a token is trusted for rate-limit
    keying. Keying on an unverified `sub` lets an unauthenticated attacker forge
    a token carrying a victim's user id and exhaust that victim's per-user limit,
    locking them out without ever authenticating. On any verification failure we
    return ``None`` so the caller falls back to keying by client IP.
    """
    if token is None:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


async def _check(
    script: AsyncScript,
    redis: aioredis.Redis,
    key: str,
    limit: int,
    window: int,
) -> tuple[bool, int]:
    """Sliding-window check — returns (is_limited, retry_after_seconds).

    Delegates to a single atomic Lua script (one round trip, no pollution, no
    member collisions).
    """
    now = time.time()
    member = f"{now}:{uuid.uuid4().hex}"
    limited, retry_after = await script(
        keys=[key],
        args=[now, window, limit, member],
        client=redis,
    )
    return bool(limited), int(retry_after)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter backed by Redis sorted sets.

    Checks specific per-route limits first, then falls back to the global limit.
    Returns 429 with a ``Retry-After`` header when a limit is exceeded.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        redis: aioredis.Redis = request.app.state.redis
        script: AsyncScript = request.app.state.rate_limit_script
        method = request.method
        path = request.url.path

        # Determine which rule applies (first match wins, then global).
        matched: _Rule | None = next(
            (r for r in _RULES if r.matches(method, path)), None
        )

        async def _run_check(rule: _Rule) -> tuple[bool, int]:
            if rule.identifier == "user":
                uid = _verified_user_id(_bearer_token(request))
                identifier = f"user:{uid}" if uid else f"ip:{_client_ip(request)}"
            else:
                identifier = f"ip:{_client_ip(request)}"
            key = f"{RATELIMIT_PREFIX}:{rule.tag}:{identifier}"
            return await _check(script, redis, key, rule.limit, rule.window)

        # Specific rule check.
        if matched is not None:
            limited, retry_after = await _run_check(matched)
            if limited:
                return _too_many(retry_after)

        # Global fallback check.
        limited, retry_after = await _run_check(_GLOBAL_RULE)
        if limited:
            return _too_many(retry_after)

        return await call_next(request)


def _too_many(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": f"Too many requests. Please retry after {retry_after} seconds.",
            "code": "rate_limit_exceeded",
        },
        headers={"Retry-After": str(retry_after)},
    )
