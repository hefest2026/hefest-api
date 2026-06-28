"""Fenced outbox claim / reaper / finalizers — the worker's data-integrity core.

This module is the **pure SQL layer** for the Postgres-outbox notification
worker (spec §3). Every function takes an explicit ``conn: BaseDBAsyncClient``
and runs exactly ONE statement on it. It deliberately does **not** manage
transactions — that responsibility belongs to the consumer (Task 7).

Transaction-boundary contract (the consumer MUST honor this)
------------------------------------------------------------
* The claim runs in a brief transaction that COMMITS immediately, releasing the
  connection BEFORE any SMTP send::

      async with in_transaction("default") as conn:
          jobs = await claim_batch(conn, worker_id, batch_size)
      # tx committed here; rows are now 'processing' and durably claimed

* Each finalizer runs in its OWN short, independent transaction::

      async with in_transaction("default") as conn:
          held = await mark_completed(conn, job.id, worker_id)
      if not held:
          ...  # lease was reclaimed mid-send: discard, never resend/restamp

The fence
---------
Every finalizer is conditional on
``id=$id AND locked_by=$worker AND status='processing'``. If the reaper (or
another worker) reclaimed the lease while this worker was sending, the row no
longer matches and the UPDATE affects **0 rows**. The finalizer then returns
``False`` and the caller MUST discard its result — never resend, never restamp.
A return of ``True`` (1 row affected) means the fence held and the terminal
state was applied. This 0-rows-to-False mapping is the hard data-integrity
guarantee of the whole worker.

``execute_query`` return shape (tortoise-orm 1.1.7, asyncpg backend)
--------------------------------------------------------------------
``await conn.execute_query(sql, params)`` returns ``tuple[int, list[dict]]``.
For an ``UPDATE``/``DELETE``/``INSERT`` the backend parses the asyncpg command
tag (e.g. ``"UPDATE 1"``) and returns ``(rows_affected, [])``. The first
element is therefore the authoritative affected-row count the fence relies on.
The claim CTE (``WITH ... UPDATE ... RETURNING``) is read with
``execute_query_dict`` since it begins with ``WITH`` and returns rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import orjson

if TYPE_CHECKING:
    import uuid

    from tortoise import BaseDBAsyncClient


@dataclass(frozen=True)
class ClaimedJob:
    """A single outbox row claimed for processing by this worker.

    Attributes:
        id: Primary key of the ``notification_jobs`` row.
        event_type: The event type that drives recipient/template selection.
        payload: The decoded jsonb payload (ids only — never PII; spec §7.4).
        idempotency_key: Per-job dedupe key carried into the send envelope.
        attempts: Post-increment attempt number for this claim (1 on first try).
    """

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    attempts: int


def _normalize_payload(payload: Any) -> dict[str, Any]:
    """Decode a raw jsonb ``payload`` into a dict.

    asyncpg returns jsonb as raw text on raw queries (no ORM decode layer), so a
    text/bytes payload is parsed with orjson; an already-decoded dict passes
    through unchanged (matching ``relay.claim_pending_jobs``).

    Args:
        payload: The raw ``payload`` value from a claimed row.

    Returns:
        The payload as a dict.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        return orjson.loads(payload)
    return payload


async def claim_batch(
    conn: BaseDBAsyncClient, worker_id: str, batch_size: int
) -> list[ClaimedJob]:
    """Claim up to ``batch_size`` due jobs for ``worker_id`` (spec §3).

    Runs the claim CTE: selects ``pending`` rows due to run (FIFO by
    ``next_attempt_at``, then ``id``) with ``FOR UPDATE SKIP LOCKED`` so
    concurrent workers never claim the same row, then flips them to
    ``processing`` — stamping the fence (``locked_by``), the heartbeat, and the
    incremented attempt counter — and returns the claimed rows.

    Must run inside a transaction the caller commits immediately (see module
    docstring). The row locks are held until that COMMIT.

    Args:
        conn: Connection bound to the caller's claim transaction.
        worker_id: This worker's fencing token, written to ``locked_by``.
        batch_size: Maximum number of rows to claim.

    Returns:
        Claimed jobs, oldest-due first; empty when no work is due.
    """
    rows = await conn.execute_query_dict(
        """
        WITH claimable AS (
            SELECT id FROM notification_jobs
            WHERE status = 'pending' AND next_attempt_at <= statement_timestamp()
            ORDER BY next_attempt_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        UPDATE notification_jobs j
        SET status='processing', locked_by=$1,
            heartbeat_at=statement_timestamp(),
            attempts=attempts+1, updated_at=statement_timestamp()
        FROM claimable WHERE j.id = claimable.id
        RETURNING j.id, j.event_type, j.payload, j.idempotency_key, j.attempts
        """,
        [worker_id, batch_size],
    )
    return [
        ClaimedJob(
            id=row["id"],
            event_type=row["event_type"],
            payload=_normalize_payload(row["payload"]),
            idempotency_key=row["idempotency_key"],
            attempts=row["attempts"],
        )
        for row in rows
    ]


async def reap_stale(conn: BaseDBAsyncClient, reaper_idle_seconds: int) -> int:
    """Reclaim leases whose heartbeat has gone stale (spec §3).

    Flips ``processing`` rows whose ``heartbeat_at`` is older than
    ``reaper_idle_seconds`` back to ``pending`` and clears the fence so they can
    be re-claimed. Deliberately does **not** touch ``attempts`` (the failed
    claim already consumed an attempt) or ``next_attempt_at`` (the row is due
    again immediately).

    Args:
        conn: Connection (caller-owned transaction).
        reaper_idle_seconds: Heartbeat staleness threshold, in seconds.

    Returns:
        Number of rows reclaimed.
    """
    affected, _ = await conn.execute_query(
        """
        UPDATE notification_jobs
        SET status='pending', locked_by=NULL, updated_at=statement_timestamp()
        WHERE status='processing'
          AND heartbeat_at
              < statement_timestamp() - make_interval(secs => $1)
        """,
        [reaper_idle_seconds],
    )
    return affected


async def mark_completed(
    conn: BaseDBAsyncClient, job_id: uuid.UUID, worker_id: str
) -> bool:
    """Finalize a successfully-sent job as ``completed``, fenced.

    Args:
        conn: Connection (its own finalizer transaction).
        job_id: The job being finalized.
        worker_id: This worker's fencing token; must still own the lease.

    Returns:
        ``True`` if the fence held (1 row updated); ``False`` if the lease was
        reclaimed (0 rows) — the caller must discard its result and not resend.
    """
    affected, _ = await conn.execute_query(
        """
        UPDATE notification_jobs
        SET status='completed', updated_at=statement_timestamp()
        WHERE id=$1 AND locked_by=$2 AND status='processing'
        """,
        [job_id, worker_id],
    )
    return affected == 1


async def mark_retry(
    conn: BaseDBAsyncClient,
    job_id: uuid.UUID,
    worker_id: str,
    last_error: str,
    delay_seconds: int,
) -> bool:
    """Release a job for a later retry with backoff, fenced.

    Returns it to ``pending``, clears the fence, records ``last_error``, and
    schedules the next attempt ``delay_seconds`` in the future.

    Args:
        conn: Connection (its own finalizer transaction).
        job_id: The job being released.
        worker_id: This worker's fencing token; must still own the lease.
        last_error: Error message recorded for diagnostics.
        delay_seconds: Backoff before the job becomes due again.

    Returns:
        ``True`` if the fence held (1 row updated); ``False`` if the lease was
        reclaimed (0 rows) — the caller must discard its result and not restamp.
    """
    affected, _ = await conn.execute_query(
        """
        UPDATE notification_jobs
        SET status='pending', locked_by=NULL, last_error=$3,
            next_attempt_at
                = statement_timestamp() + make_interval(secs => $4),
            updated_at=statement_timestamp()
        WHERE id=$1 AND locked_by=$2 AND status='processing'
        """,
        [job_id, worker_id, last_error, delay_seconds],
    )
    return affected == 1


async def mark_failed(
    conn: BaseDBAsyncClient, job_id: uuid.UUID, worker_id: str, last_error: str
) -> bool:
    """Finalize a job as terminally ``failed`` (attempts exhausted), fenced.

    Args:
        conn: Connection (its own finalizer transaction).
        job_id: The job being failed.
        worker_id: This worker's fencing token; must still own the lease.
        last_error: Final error message recorded for diagnostics.

    Returns:
        ``True`` if the fence held (1 row updated); ``False`` if the lease was
        reclaimed (0 rows) — the caller must discard its result.
    """
    affected, _ = await conn.execute_query(
        """
        UPDATE notification_jobs
        SET status='failed', last_error=$3, updated_at=statement_timestamp()
        WHERE id=$1 AND locked_by=$2 AND status='processing'
        """,
        [job_id, worker_id, last_error],
    )
    return affected == 1


def backoff_delay(attempts: int, backoff_base: int) -> int:
    """Compute the retry delay before the next attempt (spec §5).

    Exponential backoff with a base of 4: ``backoff_base * 4 ** (attempts - 1)``.
    With ``backoff_base=30`` this yields 30s, 120s, 480s for attempts 1, 2, 3.

    Args:
        attempts: The attempt number that just failed (1-based).
        backoff_base: Base delay in seconds for the first retry.

    Returns:
        Seconds to wait before the job becomes due again.
    """
    return backoff_base * (4 ** (attempts - 1))
