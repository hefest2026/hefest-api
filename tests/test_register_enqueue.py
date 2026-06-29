"""Integration test: POST /register enqueues an EmailVerify outbox job.

Exercises the real register handler against a live Postgres — the duplicate
check, ``User.create``, the enclosing transaction, and the
``NotificationJob.create(event=None, ...)`` enqueue (and therefore the nullable
``event_id`` from migration 0007). A unit-level mock cannot cover the
account-scoped insert because the value under test is precisely that the FK
accepts NULL and the row lands ``pending`` for the worker.

Skip gating mirrors ``test_worker_integration``: the module probes the DB at
load and skips entirely when Postgres is unreachable, so the no-DB CI unit job
stays green. The created ``User`` is deleted in teardown; FK ``ON DELETE
CASCADE`` removes the enqueued job with it, so dev data is never touched.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from tortoise import Tortoise
from tortoise.exceptions import DBConnectionError, OperationalError

from hefest.config import build_worker_tortoise_orm, settings
from hefest.models.notification_job import JobStatus, NotificationJob
from hefest.models.user import User
from hefest.routers.auth import register
from hefest.schemas.auth import RegisterRequest

# ---------------------------------------------------------------------------
# Module-level skip gate
# ---------------------------------------------------------------------------


async def _probe_db() -> None:
    """Connect to Postgres and execute SELECT 1; raise on failure."""
    dsn = settings.db_url.replace("asyncpg://", "postgresql://", 1)
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


try:
    asyncio.run(_probe_db())
except (OSError, asyncpg.PostgresError, DBConnectionError, OperationalError):
    pytest.skip("integration DB unavailable", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db() -> AsyncIterator[None]:
    """Initialise Tortoise for one test and close all connections on teardown."""
    await Tortoise.init(config=build_worker_tortoise_orm())
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _register_body() -> RegisterRequest:
    """Build a RegisterRequest with a unique email (safe to delete)."""
    return RegisterRequest(
        email=f"reg-{uuid.uuid4().hex[:8]}@example.com",
        password="correct-horse-battery",  # >= 12 chars
        full_name="Integration Register",
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
async def test_register_enqueues_pending_email_verify_job() -> None:
    body = _register_body()
    user: User | None = None
    try:
        await register(body)

        user = await User.get_or_none(email=body.email)
        assert user is not None
        assert user.email_verified_at is None

        jobs = await NotificationJob.filter(idempotency_key=f"{user.id}:EmailVerify")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.event_id is None
        assert job.event_type == "EmailVerify"
        assert job.status is JobStatus.pending
        assert job.payload == {"student_id": str(user.id)}
    finally:
        if user is not None:
            await user.delete()  # CASCADE removes the enqueued job
