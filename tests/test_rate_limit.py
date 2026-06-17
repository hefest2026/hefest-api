"""Tests for the Redis sliding-window rate limiter.

Covers the security and correctness fixes raised in code review:
- forged/unverified JWTs must not key a victim's per-user limit,
- blocked requests must not pollute the window (no indefinite lockout),
- the window must recover once requests age out,
- each request is counted distinctly (no member collisions).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest
from jose import jwt

from hefest.config import settings
from hefest.middleware.rate_limit import (
    SLIDING_WINDOW_LUA,
    _check,
    _verified_user_id,
)


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def script(redis: fakeredis.aioredis.FakeRedis):
    return redis.register_script(SLIDING_WINDOW_LUA)


async def test_allows_requests_under_limit(redis, script) -> None:
    for _ in range(3):
        limited, retry_after = await _check(script, redis, "k", limit=3, window=60)
        assert limited is False
        assert retry_after == 0


async def test_blocks_when_limit_exceeded(redis, script) -> None:
    for _ in range(2):
        await _check(script, redis, "k", limit=2, window=60)

    limited, retry_after = await _check(script, redis, "k", limit=2, window=60)
    assert limited is True
    assert retry_after >= 1


async def test_blocked_requests_do_not_pollute_window(redis, script) -> None:
    # Hammer well past the limit; blocked attempts must NOT be recorded,
    # otherwise the window stays permanently full -> indefinite lockout.
    for _ in range(10):
        await _check(script, redis, "k", limit=2, window=60)

    assert await redis.zcard("k") == 2


async def test_window_recovers_after_entries_age_out(redis, script) -> None:
    for _ in range(2):
        await _check(script, redis, "k", limit=2, window=1)
    blocked, _ = await _check(script, redis, "k", limit=2, window=1)
    assert blocked is True

    await asyncio.sleep(1.1)

    limited, _ = await _check(script, redis, "k", limit=2, window=1)
    assert limited is False


async def test_each_request_counted_distinctly(redis, script) -> None:
    # Unique members mean N allowed calls produce exactly N entries even when
    # issued back-to-back (no same-timestamp collisions collapsing the count).
    for _ in range(5):
        await _check(script, redis, "k", limit=100, window=60)
    assert await redis.zcard("k") == 5


def _token(sub: str, secret: str) -> str:
    return jwt.encode({"sub": sub}, secret, algorithm=settings.jwt_algorithm)


def test_verified_user_id_accepts_valid_token() -> None:
    token = _token("user-42", settings.jwt_secret)
    assert _verified_user_id(token) == "user-42"


def test_verified_user_id_rejects_forged_signature() -> None:
    # Attacker forges a victim's sub but cannot sign with the server secret.
    forged = _token("victim-1", "attacker-guessed-secret")
    assert _verified_user_id(forged) is None


def test_verified_user_id_rejects_alg_none_token() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "victim-1"}).encode()).rstrip(
        b"="
    )
    unsigned = f"{header.decode()}.{payload.decode()}."
    assert _verified_user_id(unsigned) is None


def test_verified_user_id_handles_missing_or_malformed() -> None:
    assert _verified_user_id(None) is None
    assert _verified_user_id("not-a-jwt") is None
