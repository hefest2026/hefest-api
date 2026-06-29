"""Integration tests for the notification worker against a live Postgres.

These tests exercise the real SQL claim/reap/finalize paths, the per-job
decision matrix in the consumer, and the cancel-event fan-out against a real
database.  A ``StubMailer`` replaces the real SMTP mailer so sends are
deterministic and no network is needed.

Database
--------
The ``db`` fixture (conftest.py) provides an ephemeral ``postgres:16-alpine``
container spun up once per session via testcontainers, with the schema created
by ``generate_schemas``. The tests therefore run in CI wherever Docker is
available and skip only when no Docker daemon is reachable.

Isolation
---------
Each test creates its own ``User`` / ``Event`` / ``Registration`` /
``NotificationJob`` rows with fresh UUIDs and deletes exactly those ``User``
rows in teardown.  FK ``ON DELETE CASCADE`` (user→events→registrations/
notification_jobs) cleans every derived row automatically.  No ``TRUNCATE``
is used.

Row-scoped claims
-----------------
Tests use ``_claim_job_by_id`` to claim only their own specific rows rather
than the broad ``claim_batch`` / ``_drain`` helpers.  Tests 2 and 4 use
``claim_batch`` only against jobs they inserted (test 2 does one job; test 4
uses n=20 jobs on a fresh event whose jobs are the only pending rows on that
event).  The database starts empty, so this scoping is defensive, not required.

HTTP-layer note
---------------
Asserting the JSON shape returned by ``GET /notification-jobs/{id}`` would
require FastAPI test-client setup with JWT auth and is out of scope here.
Instead, test 1 reads the exact ORM fields the router surfaces (``status`` +
``last_error``) directly from the DB, verifying the state the endpoint would
return.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import orjson
from tortoise.transactions import in_transaction

from hefest.config import settings
from hefest.models.event import Event, EventStatus
from hefest.models.notification_job import JobStatus, NotificationJob
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User, UserRole
from hefest.services.event import cancel_event
from hefest.worker import consumer
from hefest.worker.claim import (
    ClaimedJob,
    backoff_delay,
    claim_batch,
    mark_completed,
    reap_stale,
)
from hefest.worker.mailer import TransientSendError
from hefest.worker.templates import EmailContent

# The ephemeral Postgres + Tortoise lifecycle is owned by the session-scoped
# ``pg_container`` / ``db`` fixtures in conftest.py. Tests opt in via ``db``;
# Docker-unavailable runs skip there with a clear reason.


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubMailer:
    """Deterministic mailer stub that records sends without hitting SMTP.

    Attributes:
        sent: ``(to, subject)`` pairs for every successful ``send`` call.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        """Initialise the stub.

        Args:
            error: If set, every ``send`` call raises this exception instead of
                recording the send.
        """
        self.error = error
        self.sent: list[tuple[str, str]] = []

    async def send(self, content: EmailContent, to: str) -> None:
        """Record a send or raise the configured error.

        Args:
            content: Rendered email content (subject + body).
            to: Recipient email address.

        Raises:
            Exception: Whatever was supplied as ``error`` at construction time.
        """
        if self.error is not None:
            raise self.error
        self.sent.append((to, content.subject))

    async def aclose(self) -> None:
        """No-op; no real connection to close."""


class _StubHeartbeat:
    """Heartbeat stub whose ``lease_lost`` event is never set."""

    def __init__(self) -> None:
        self.lease_lost: asyncio.Event = asyncio.Event()


# ---------------------------------------------------------------------------
# Data factories
# ---------------------------------------------------------------------------


def _unique_email(prefix: str = "user") -> str:
    """Return a unique email address scoped to integration tests."""
    return f"{prefix}+{uuid.uuid4().hex[:8]}@inttest.hefest.local"


async def _create_organizer() -> User:
    """Insert a fresh organizer user."""
    return await User.create(
        email=_unique_email("org"),
        full_name="Integration Organizer",
        role=UserRole.organizer,
    )


async def _create_student() -> User:
    """Insert a fresh student user."""
    return await User.create(
        email=_unique_email("stu"),
        full_name="Integration Student",
        role=UserRole.student,
    )


async def _create_event(
    organizer: User,
    *,
    status: EventStatus = EventStatus.published,
) -> Event:
    """Insert a fresh event in the future owned by ``organizer``."""
    return await Event.create(
        organizer=organizer,
        title=f"Inttest Event {uuid.uuid4().hex[:6]}",
        description="Integration test event — safe to delete",
        starts_at=datetime.now(UTC) + timedelta(days=7),
        location="Test Hall A",
        capacity=100,
        status=status,
    )


async def _create_registration(
    student: User,
    event: Event,
    *,
    status: RegistrationStatus = RegistrationStatus.confirmed,
) -> Registration:
    """Insert a registration for ``student`` at ``event``."""
    return await Registration.create(
        student=student,
        event=event,
        status=status,
    )


async def _enqueue_job(
    event: Event,
    student: User,
    *,
    event_type: str = "RegistrationConfirmed",
) -> NotificationJob:
    """Insert a pending ``NotificationJob`` for ``student`` / ``event``."""
    return await NotificationJob.create(
        event=event,
        event_type=event_type,
        payload={
            "event_id": str(event.id),
            "student_id": str(student.id),
        },
        idempotency_key=(f"{event.id}:{student.id}:{event_type}:{uuid.uuid4().hex}"),
    )


def _normalize_payload(payload: Any) -> dict[str, Any]:
    """Decode a raw jsonb payload returned by execute_query_dict.

    asyncpg may return jsonb as a raw string on raw queries.

    Args:
        payload: Raw payload value from a query result row.

    Returns:
        Decoded dict.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        return orjson.loads(payload)
    return dict(payload)


async def _claim_job_by_id(job_id: uuid.UUID, worker_id: str) -> ClaimedJob | None:
    """Claim a specific pending job by ID (test-isolation helper).

    Unlike the broad ``claim_batch`` this targets one row by primary key so
    unrelated pending rows in dev data are never touched.

    Args:
        job_id: Primary key of the ``NotificationJob`` to claim.
        worker_id: Fencing token written to ``locked_by``.

    Returns:
        The claimed job, or ``None`` if the row is not currently claimable.
    """
    async with in_transaction("default") as conn:
        rows = await conn.execute_query_dict(
            """
            UPDATE notification_jobs
            SET status='processing', locked_by=$1,
                heartbeat_at=statement_timestamp(),
                attempts=attempts+1, updated_at=statement_timestamp()
            WHERE id=$2
              AND status='pending'
              AND next_attempt_at <= statement_timestamp()
            RETURNING id, event_type, payload, idempotency_key, attempts
            """,
            [worker_id, job_id],
        )
    if not rows:
        return None
    row = rows[0]
    return ClaimedJob(
        id=row["id"],
        event_type=row["event_type"],
        payload=_normalize_payload(row["payload"]),
        idempotency_key=row["idempotency_key"],
        attempts=row["attempts"],
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path: enqueue → claim → send → completed
# ---------------------------------------------------------------------------


async def test_happy_path_job_completes(db: Any) -> None:
    """A pending job reaches 'completed' and the stub records one send.

    Uses ``_claim_job_by_id`` to target only the test-owned row, leaving any
    unrelated pending dev-data jobs untouched.

    Assertions mirror exactly what ``GET /notification-jobs/{id}`` surfaces
    (``status`` + ``last_error``); HTTP-layer / auth testing is out of scope
    (see module docstring).
    """
    organizer = await _create_organizer()
    student = await _create_student()
    event = await _create_event(organizer)
    await _create_registration(student, event)
    job = await _enqueue_job(event, student)

    worker_id = f"inttest-worker:{uuid.uuid4()}"
    mailer = StubMailer()

    claimed = await _claim_job_by_id(job.id, worker_id)
    assert claimed is not None, "job should be immediately claimable"
    assert claimed.attempts == 1

    await consumer._process_one(claimed, worker_id, cast(Any, mailer))

    # Verify DB state — mirrors notification_jobs endpoint fields
    refreshed = await NotificationJob.get(id=job.id)
    assert refreshed.status == JobStatus.completed
    assert refreshed.last_error is None

    # Stub recorded exactly one send to the student
    assert len(mailer.sent) == 1
    to_addr, subject = mailer.sent[0]
    assert to_addr == student.email
    assert event.title in subject

    await User.filter(id__in=[organizer.id, student.id]).delete()


# ---------------------------------------------------------------------------
# Test 2 — crash simulation + fencing
# ---------------------------------------------------------------------------


async def test_fencing_dead_worker_finalizer_is_noop(db: Any) -> None:
    """A dead worker's fenced mark_completed returns False and is a no-op.

    Flow:
    1. Claim as worker A (attempts → 1).
    2. Fake-stale the heartbeat (set 1 hour in the past).
    3. ``reap_stale`` reclaims the lease; assert ``status='pending'``,
       ``locked_by IS NULL``, and ``attempts`` unchanged (still 1).
    4. Claim as worker B (attempts → 2).
    5. Worker A's ``mark_completed`` returns ``False``; row stays owned by B.
    """
    organizer = await _create_organizer()
    student = await _create_student()
    event = await _create_event(organizer)
    job = await _enqueue_job(event, student)

    worker_a = f"inttest-A:{uuid.uuid4()}"
    worker_b = f"inttest-B:{uuid.uuid4()}"

    # Step 1 — A claims (attempts → 1)
    async with in_transaction("default") as conn:
        claimed_a = await claim_batch(conn, worker_a, 10)
    a_job = next((j for j in claimed_a if j.id == job.id), None)
    assert a_job is not None, "job should be in claimed batch"
    assert a_job.attempts == 1

    # Step 2 — fake a stale heartbeat (1 hour ago)
    await NotificationJob.filter(id=job.id).update(
        heartbeat_at=datetime.now(UTC) - timedelta(hours=1)
    )

    # Step 3 — reaper reclaims; attempts must be unchanged
    async with in_transaction("default") as conn:
        reaped = await reap_stale(conn, 300, 1000)
    assert reaped >= 1

    row = await NotificationJob.get(id=job.id)
    assert row.status == JobStatus.pending
    assert row.locked_by is None
    assert row.attempts == 1  # reaper MUST NOT touch attempts

    # Step 4 — B claims (attempts → 2)
    async with in_transaction("default") as conn:
        claimed_b = await claim_batch(conn, worker_b, 10)
    b_job = next((j for j in claimed_b if j.id == job.id), None)
    assert b_job is not None
    assert b_job.attempts == 2

    # Step 5 — A's stale finalize must be a no-op (0 rows → False)
    async with in_transaction("default") as conn:
        held = await mark_completed(conn, job.id, worker_a)
    assert held is False

    # Row still owned by B and still processing
    still = await NotificationJob.get(id=job.id)
    assert still.locked_by == worker_b
    assert still.status == JobStatus.processing

    await User.filter(id__in=[organizer.id, student.id]).delete()


# ---------------------------------------------------------------------------
# Test 3 — retry / backoff then failed at max_attempts
# ---------------------------------------------------------------------------


async def test_retry_backoff_then_failed_at_max_attempts(db: Any) -> None:
    """Transient errors back off per attempt; final attempt drives mark_failed.

    For each attempt up to ``worker_max_attempts``:
    - Force ``next_attempt_at`` to the past so the job is claimable.
    - Run ``_process_one`` with a TransientSendError mailer.
    - Before the last attempt: assert ``status='pending'``, ``last_error`` set,
      and ``next_attempt_at`` in the future (job not yet due again).
    - On the last attempt: assert ``status='failed'`` with ``last_error`` set.
    """
    organizer = await _create_organizer()
    student = await _create_student()
    event = await _create_event(organizer)
    await _create_registration(student, event)
    job = await _enqueue_job(event, student)

    worker_id = f"inttest-worker:{uuid.uuid4()}"
    max_attempts = settings.worker_max_attempts
    error = TransientSendError("simulated transient SMTP failure")
    mailer = StubMailer(error=error)

    for attempt in range(1, max_attempts + 1):
        # Force the job to be immediately due
        await NotificationJob.filter(id=job.id).update(
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)
        )

        claimed_job = await _claim_job_by_id(job.id, worker_id)
        assert claimed_job is not None, f"job not claimable on attempt {attempt}"
        assert claimed_job.attempts == attempt

        await consumer._process_one(claimed_job, worker_id, cast(Any, mailer))

        row = await NotificationJob.get(id=job.id)

        if attempt < max_attempts:
            assert row.status == JobStatus.pending, (
                f"expected pending after attempt {attempt}, got {row.status}"
            )
            assert row.last_error is not None
            assert row.next_attempt_at is not None
            assert row.next_attempt_at > datetime.now(UTC), (
                "next_attempt_at must be in the future after a transient failure"
            )
            # Verify the delay matches the expected backoff formula
            expected_delay = backoff_delay(
                attempt, settings.worker_backoff_base_seconds
            )
            lower_bound = datetime.now(UTC) + timedelta(seconds=expected_delay - 2)
            assert row.next_attempt_at >= lower_bound, (
                f"backoff too short: expected >= {expected_delay}s delay"
            )
            # Direct DB check: job is not yet due (avoids claiming dev-data rows)
            not_due = await NotificationJob.filter(
                id=job.id,
                status=JobStatus.pending,
            ).first()
            assert not_due is not None
            assert not_due.next_attempt_at is not None
            assert not_due.next_attempt_at > datetime.now(UTC)
        else:
            assert row.status == JobStatus.failed
            assert row.last_error is not None

    await User.filter(id__in=[organizer.id, student.id]).delete()


# ---------------------------------------------------------------------------
# Test 4 — horizontal scaling: disjoint concurrent claims
# ---------------------------------------------------------------------------


async def test_concurrent_claimers_produce_disjoint_sets(db: Any) -> None:
    """Two concurrent claim_batch calls in separate transactions never overlap.

    ``FOR UPDATE SKIP LOCKED`` guarantees that concurrent workers cannot claim
    the same row.  Two asyncio-concurrent claimers — each in its own pooled
    connection / transaction — must return disjoint id sets that together cover
    ≤ N rows with no duplicates.
    """
    organizer = await _create_organizer()
    student = await _create_student()
    event = await _create_event(organizer)

    n_jobs = 20
    batch_size = 15  # each requests 15/20 — without SKIP LOCKED they'd overlap

    for _ in range(n_jobs):
        await _enqueue_job(event, student)

    worker_a = f"inttest-A:{uuid.uuid4()}"
    worker_b = f"inttest-B:{uuid.uuid4()}"

    async def _claim(wid: str) -> list[uuid.UUID]:
        """Claim a batch in its own committed transaction."""
        async with in_transaction("default") as conn:
            batch = await claim_batch(conn, wid, batch_size)
        return [j.id for j in batch]

    ids_a, ids_b = await asyncio.gather(_claim(worker_a), _claim(worker_b))

    set_a = set(ids_a)
    set_b = set(ids_b)

    assert set_a.isdisjoint(set_b), f"duplicate claims between A and B: {set_a & set_b}"
    assert len(set_a) + len(set_b) <= n_jobs

    await User.filter(id__in=[organizer.id, student.id]).delete()


# ---------------------------------------------------------------------------
# Test 5 — EventCancelled fan-out end-to-end
# ---------------------------------------------------------------------------


async def test_event_cancelled_fanout_and_drain_delivers_all(db: Any) -> None:
    """cancel_event fans out K jobs; claiming and processing all delivers them.

    Creates K registrations (confirmed + waitlisted), cancels the event,
    asserts K ``EventCancelled`` notification_jobs were enqueued, then claims
    and processes each job with ``_claim_job_by_id`` + ``_process_one``
    (targeting only test-owned rows to leave dev data untouched).  Verifies
    all K reach ``status='completed'`` with exactly K sends on the stub.
    """
    organizer = await _create_organizer()
    event = await _create_event(organizer)

    k_confirmed = 3
    k_waitlisted = 2
    students: list[User] = []

    for _ in range(k_confirmed):
        student = await _create_student()
        students.append(student)
        await _create_registration(student, event, status=RegistrationStatus.confirmed)

    for _ in range(k_waitlisted):
        student = await _create_student()
        students.append(student)
        await _create_registration(student, event, status=RegistrationStatus.waitlisted)

    k = k_confirmed + k_waitlisted

    # Fan-out: cancel enqueues one EventCancelled job per active registration
    await cancel_event(organizer, event.id)

    jobs = await NotificationJob.filter(event_id=event.id, event_type="EventCancelled")
    assert len(jobs) == k, f"expected {k} EventCancelled jobs, got {len(jobs)}"

    # Claim and process each test-owned job individually
    worker_id = f"inttest-worker:{uuid.uuid4()}"
    mailer = StubMailer()

    for job in jobs:
        claimed = await _claim_job_by_id(job.id, worker_id)
        assert claimed is not None, f"EventCancelled job {job.id} not claimable"
        await consumer._process_one(claimed, worker_id, cast(Any, mailer))

    # All K jobs must be completed
    completed_jobs = await NotificationJob.filter(
        event_id=event.id, event_type="EventCancelled"
    )
    for rj in completed_jobs:
        assert rj.status == JobStatus.completed, (
            f"job {rj.id} is {rj.status}, last_error={rj.last_error}"
        )

    # Stub recorded exactly K sends (one per student)
    assert len(mailer.sent) == k

    student_ids = [s.id for s in students]
    await User.filter(id__in=[organizer.id, *student_ids]).delete()
