"""Tests for the outbox-to-Redis relay (HEF-16).

Covers the relay's pure transforms and its Redis/DB I/O units:
- envelope construction carries ids only (no PII),
- jsonb payloads returned as text are normalised to dicts on claim,
- a claim batch is published to the stream as one orjson envelope per row,
- published rows are marked via a parameterised UPDATE,
- drain orchestrates claim -> publish -> mark and short-circuits when idle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import orjson
import pytest

from hefest.worker import relay


def _row(
    job_id: str = "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
    event_type: str = "RegistrationConfirmed",
    payload: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    if payload is None:
        payload = {
            "event_id": "e1",
            "user_id": "u1",
            "occurred_at": "2026-06-16T14:00:00Z",
        }
    return {
        "id": job_id,
        "event_type": event_type,
        "payload": payload,
        "idempotency_key": f"{job_id}:{event_type}",
    }


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


def test_build_envelope_carries_ids_only() -> None:
    row = _row(payload={"event_id": "e1", "user_id": "u1"})

    envelope = relay.build_envelope(row)

    assert envelope == {
        "type": "RegistrationConfirmed",
        "idempotency_key": row["idempotency_key"],
        "event_id": "e1",
        "user_id": "u1",
    }
    assert "payload" not in envelope


async def test_claim_pending_jobs_normalises_text_payload() -> None:
    raw_payload = orjson.dumps({"event_id": "e1"}).decode()
    conn = AsyncMock()
    conn.execute_query_dict.return_value = [_row(payload=raw_payload)]

    rows = await relay.claim_pending_jobs(conn, batch_size=100)

    assert rows[0]["payload"] == {"event_id": "e1"}
    query, values = conn.execute_query_dict.call_args.args
    assert "FOR UPDATE SKIP LOCKED" in query
    assert values == [100]


async def test_claim_pending_jobs_passes_dict_payload_through() -> None:
    conn = AsyncMock()
    conn.execute_query_dict.return_value = [_row(payload={"event_id": "e1"})]

    rows = await relay.claim_pending_jobs(conn, batch_size=10)

    assert rows[0]["payload"] == {"event_id": "e1"}


async def test_publish_batch_xadds_one_envelope_per_row(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    rows = [_row(job_id="a", payload={"event_id": "e1"}), _row(job_id="b")]

    await relay.publish_batch(redis, stream="s", maxlen=10_000, rows=rows)

    entries = await redis.xrange("s")
    assert entries is not None
    assert len(entries) == len(rows)
    fields = entries[0][1]
    assert fields is not None
    first = orjson.loads(fields[relay.MESSAGE_FIELD.encode()])
    assert first["type"] == "RegistrationConfirmed"
    assert first["idempotency_key"] == "a:RegistrationConfirmed"


async def test_publish_batch_no_rows_is_noop(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await relay.publish_batch(redis, stream="s", maxlen=10_000, rows=[])

    assert await redis.xlen("s") == 0


async def test_mark_published_updates_claimed_ids() -> None:
    conn = AsyncMock()
    ids = ["a", "b"]

    await relay.mark_published(conn, ids)

    query, values = conn.execute_query.call_args.args
    assert "status = 'published'" in query
    assert values == [ids]


async def test_drain_orchestrates_claim_publish_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_row(job_id="a"), _row(job_id="b")]
    published: list[list[dict[str, Any]]] = []
    marked: list[list[Any]] = []

    monkeypatch.setattr(relay, "in_transaction", _fake_in_transaction)
    monkeypatch.setattr(relay, "claim_pending_jobs", AsyncMock(return_value=rows))

    async def fake_publish(
        _redis: Any, _s: str, _m: int, r: list[dict[str, Any]]
    ) -> None:
        published.append(r)

    async def fake_mark(_conn: Any, ids: list[Any]) -> None:
        marked.append(ids)

    monkeypatch.setattr(relay, "publish_batch", fake_publish)
    monkeypatch.setattr(relay, "mark_published", fake_mark)

    count = await relay.drain(redis=AsyncMock(), batch_size=100, stream="s", maxlen=1)

    assert count == 2
    assert published == [rows]
    assert marked == [["a", "b"]]


async def test_drain_returns_zero_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, "in_transaction", _fake_in_transaction)
    monkeypatch.setattr(relay, "claim_pending_jobs", AsyncMock(return_value=[]))
    publish = AsyncMock()
    monkeypatch.setattr(relay, "publish_batch", publish)

    count = await relay.drain(redis=AsyncMock(), batch_size=100, stream="s", maxlen=1)

    assert count == 0
    publish.assert_not_called()


class _FakeAtomic:
    """Minimal async-context stand-in for tortoise.in_transaction()."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _fake_in_transaction(_name: str) -> _FakeAtomic:
    return _FakeAtomic()
