"""Unit tests for the fenced claim/reaper/finalizer SQL layer (HEF-39, spec §3).

The connection is mocked with :class:`unittest.mock.AsyncMock`. ``execute_query``
returns the real tortoise-orm 1.1.7 asyncpg shape ``tuple[int, list[dict]]`` so
the affected-row-count to bool mapping is exercised exactly as in production —
in particular the 0-rows-to-``False`` lease-lost path, the core data-integrity
guarantee.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import orjson
import pytest

from hefest.worker import claim

WORKER_ID = "worker-1"
JOB_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _row(payload: Any) -> dict[str, Any]:
    """Build a claim-CTE result row with the given (raw) payload."""
    return {
        "id": JOB_ID,
        "event_type": "EventCreated",
        "payload": payload,
        "idempotency_key": "idem-1",
        "attempts": 1,
    }


# --- claim_batch -----------------------------------------------------------


async def test_claim_batch_sql_shape_and_params() -> None:
    conn = AsyncMock()
    conn.execute_query_dict.return_value = []

    await claim.claim_batch(conn, WORKER_ID, 50)

    query, values = conn.execute_query_dict.call_args.args
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "ORDER BY next_attempt_at, id" in query
    assert "status='processing'" in query
    assert "attempts=attempts+1" in query
    assert "RETURNING" in query
    assert values == [WORKER_ID, 50]


async def test_claim_batch_normalizes_text_payload() -> None:
    conn = AsyncMock()
    raw = orjson.dumps({"event_id": "e1"}).decode()
    conn.execute_query_dict.return_value = [_row(payload=raw)]

    jobs = await claim.claim_batch(conn, WORKER_ID, 10)

    assert jobs == [
        claim.ClaimedJob(
            id=JOB_ID,
            event_type="EventCreated",
            payload={"event_id": "e1"},
            idempotency_key="idem-1",
            attempts=1,
        )
    ]


async def test_claim_batch_passes_dict_payload_through() -> None:
    conn = AsyncMock()
    conn.execute_query_dict.return_value = [_row(payload={"event_id": "e1"})]

    jobs = await claim.claim_batch(conn, WORKER_ID, 10)

    assert jobs[0].payload == {"event_id": "e1"}


# --- reap_stale ------------------------------------------------------------


async def test_reap_stale_sql_shape_and_returns_count() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (3, [])

    reclaimed = await claim.reap_stale(conn, 90)

    query, values = conn.execute_query.call_args.args
    assert "status='pending'" in query
    assert "locked_by=NULL" in query
    assert "status='processing'" in query
    assert "heartbeat_at" in query
    assert "make_interval(secs => $1)" in query
    assert "attempts" not in query
    assert "next_attempt_at" not in query
    assert values == [90]
    assert reclaimed == 3


# --- finalizers: fence held (1 row) vs lease lost (0 rows) -----------------


async def test_mark_completed_fence_and_held() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (1, [])

    held = await claim.mark_completed(conn, JOB_ID, WORKER_ID)

    query, values = conn.execute_query.call_args.args
    assert "status='completed'" in query
    assert "id=$1 AND locked_by=$2 AND status='processing'" in query
    assert values == [JOB_ID, WORKER_ID]
    assert held is True


async def test_mark_completed_lease_lost_returns_false() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (0, [])

    held = await claim.mark_completed(conn, JOB_ID, WORKER_ID)

    assert held is False


async def test_mark_retry_fence_and_held() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (1, [])

    held = await claim.mark_retry(conn, JOB_ID, WORKER_ID, "boom", 120)

    query, values = conn.execute_query.call_args.args
    assert "status='pending'" in query
    assert "locked_by=NULL" in query
    assert "last_error=$3" in query
    assert "make_interval(secs => $4)" in query
    assert "id=$1 AND locked_by=$2 AND status='processing'" in query
    assert values == [JOB_ID, WORKER_ID, "boom", 120]
    assert held is True


async def test_mark_retry_lease_lost_returns_false() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (0, [])

    held = await claim.mark_retry(conn, JOB_ID, WORKER_ID, "boom", 120)

    assert held is False


async def test_mark_failed_fence_and_held() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (1, [])

    held = await claim.mark_failed(conn, JOB_ID, WORKER_ID, "fatal")

    query, values = conn.execute_query.call_args.args
    assert "status='failed'" in query
    assert "last_error=$3" in query
    assert "id=$1 AND locked_by=$2 AND status='processing'" in query
    assert values == [JOB_ID, WORKER_ID, "fatal"]
    assert held is True


async def test_mark_failed_lease_lost_returns_false() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (0, [])

    held = await claim.mark_failed(conn, JOB_ID, WORKER_ID, "fatal")

    assert held is False


# --- backoff_delay ---------------------------------------------------------


@pytest.mark.parametrize(("attempts", "expected"), [(1, 30), (2, 120), (3, 480)])
def test_backoff_delay(attempts: int, expected: int) -> None:
    assert claim.backoff_delay(attempts, 30) == expected
