"""Integration test: POST /register enqueues an EmailVerify outbox job.

Exercises the real register handler against a live Postgres — the duplicate
check, ``User.create``, the enclosing transaction, and the
``NotificationJob.create(event=None, ...)`` enqueue (and therefore the nullable
``event_id`` from migration 0007). A unit-level mock cannot cover the
account-scoped insert because the value under test is precisely that the FK
accepts NULL and the row lands ``pending`` for the worker.

The ``db`` fixture (conftest.py) provides an ephemeral testcontainers Postgres,
so this runs in CI wherever Docker is available. The created ``User`` is deleted
in teardown; FK ``ON DELETE CASCADE`` removes the enqueued job with it.
"""

from __future__ import annotations

import uuid

import pytest

from hefest.models.notification_job import JobStatus, NotificationJob
from hefest.models.user import User
from hefest.routers.auth import register
from hefest.schemas.auth import RegisterRequest

pytestmark = pytest.mark.integration


def _register_body() -> RegisterRequest:
    """Build a RegisterRequest with a unique email (safe to delete)."""
    return RegisterRequest(
        email=f"reg-{uuid.uuid4().hex[:8]}@example.com",
        password="correct-horse-battery",  # >= 12 chars
        full_name="Integration Register",
    )


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
