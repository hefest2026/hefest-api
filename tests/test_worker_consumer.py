"""Unit tests for the outbox consumer (HEF-39, spec §3-§6).

These tests exercise the pure orchestration and decision logic with mocks — the
per-job decision matrix (:func:`hefest.worker.consumer._process_one`) and the
drain loop (:func:`hefest.worker.consumer._drain`). Every collaborator is
mocked: ``recipients.load``, ``templates.render``, the ``Mailer``, the fenced
finalizers, ``reap_stale``/``claim_batch``, and ``in_transaction``. The full
LISTEN/NOTIFY + signal wiring in :func:`run` is covered by the Task 10
integration tests; driving the live LISTEN loop here would add no unit-level
value, so it is intentionally left out (noted in the task report).

Assertions target real behavior: which finalizer ran and with which arguments,
how many times the reaper/claim ran, that ``asyncio.sleep(0)`` yields between
iterations, and that the send concurrency cap is honored — never mock noise.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from hefest.worker import consumer
from hefest.worker.claim import ClaimedJob
from hefest.worker.errors import PermanentError, RecipientNotFound
from hefest.worker.heartbeat import Heartbeat
from hefest.worker.mailer import PermanentSendError, TransientSendError
from hefest.worker.templates import EmailContent

WORKER_ID = "host:00000000-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeTx:
    """Async-context stand-in for ``in_transaction(...)`` yielding a conn."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _fake_in_transaction(conn: object) -> Callable[[str], _FakeTx]:
    """Build an ``in_transaction`` replacement yielding ``conn``."""
    return lambda _name: _FakeTx(conn)


@dataclass(frozen=True)
class _User:
    email: str


@dataclass(frozen=True)
class _Recipient:
    user: _User
    event: object


def _job(attempts: int = 1) -> ClaimedJob:
    """Build a ClaimedJob with a unique id for assertions."""
    return ClaimedJob(
        id=uuid.uuid4(),
        event_type="RegistrationConfirmed",
        payload={"student_id": "s1", "event_id": "e1"},
        idempotency_key="k1",
        attempts=attempts,
    )


@pytest.fixture
def finalizers(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Replace the three fenced finalizers with fence-holding AsyncMocks."""
    mocks = {
        name: AsyncMock(return_value=True, __name__=name)
        for name in ("mark_completed", "mark_retry", "mark_failed")
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(consumer, name, mock)
    monkeypatch.setattr(consumer, "in_transaction", _fake_in_transaction(object()))
    return mocks


@pytest.fixture
def recipient_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch recipients.load + templates.render to succeed."""
    recipient = _Recipient(user=_User(email="a@b.c"), event=object())
    monkeypatch.setattr(consumer.recipients, "load", AsyncMock(return_value=recipient))
    monkeypatch.setattr(
        consumer.templates,
        "render",
        lambda *_args, **_kw: EmailContent(subject="s", body="b"),
    )


# --------------------------------------------------------------------------- #
# _process_one decision matrix
# --------------------------------------------------------------------------- #
async def test_success_marks_completed(
    finalizers: dict[str, AsyncMock], recipient_ok: None
) -> None:
    job = _job()
    mailer = AsyncMock()

    await consumer._process_one(job, WORKER_ID, mailer)

    mailer.send.assert_awaited_once()
    finalizers["mark_completed"].assert_awaited_once()
    # Fenced finalizer called as (conn, job_id, worker_id) with no extra kwargs.
    completed_call = finalizers["mark_completed"].await_args
    assert completed_call is not None
    assert completed_call.args[1:] == (job.id, WORKER_ID)
    assert completed_call.kwargs == {}
    finalizers["mark_retry"].assert_not_awaited()
    finalizers["mark_failed"].assert_not_awaited()


async def test_recipient_not_found_marks_failed(
    finalizers: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        consumer.recipients,
        "load",
        AsyncMock(side_effect=RecipientNotFound("gone")),
    )
    job = _job()
    mailer = AsyncMock()

    await consumer._process_one(job, WORKER_ID, mailer)

    mailer.send.assert_not_awaited()
    finalizers["mark_failed"].assert_awaited_once()
    failed_call = finalizers["mark_failed"].await_args
    assert failed_call is not None
    assert failed_call.kwargs["last_error"] == "gone"
    finalizers["mark_completed"].assert_not_awaited()
    finalizers["mark_retry"].assert_not_awaited()


async def test_permanent_render_error_marks_failed(
    finalizers: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    recipient = _Recipient(user=_User(email="a@b.c"), event=object())
    monkeypatch.setattr(consumer.recipients, "load", AsyncMock(return_value=recipient))

    def _boom(*_args: Any, **_kw: Any) -> EmailContent:
        raise PermanentError("unknown type")

    monkeypatch.setattr(consumer.templates, "render", _boom)
    job = _job()
    mailer = AsyncMock()

    await consumer._process_one(job, WORKER_ID, mailer)

    mailer.send.assert_not_awaited()
    finalizers["mark_failed"].assert_awaited_once()


async def test_permanent_send_error_marks_failed(
    finalizers: dict[str, AsyncMock], recipient_ok: None
) -> None:
    job = _job()
    mailer = AsyncMock()
    mailer.send.side_effect = PermanentSendError("5xx")

    await consumer._process_one(job, WORKER_ID, mailer)

    finalizers["mark_failed"].assert_awaited_once()
    finalizers["mark_retry"].assert_not_awaited()
    finalizers["mark_completed"].assert_not_awaited()


async def test_transient_under_cap_marks_retry_with_backoff(
    finalizers: dict[str, AsyncMock], recipient_ok: None
) -> None:
    job = _job(attempts=1)
    mailer = AsyncMock()
    mailer.send.side_effect = TransientSendError("timeout")

    await consumer._process_one(job, WORKER_ID, mailer)

    finalizers["mark_retry"].assert_awaited_once()
    # attempts=1, base=30 -> 30 (spec §5 exponential backoff base 4).
    retry_call = finalizers["mark_retry"].await_args
    assert retry_call is not None
    assert retry_call.kwargs["delay_seconds"] == 30
    assert retry_call.kwargs["last_error"] == "timeout"
    finalizers["mark_failed"].assert_not_awaited()


async def test_transient_at_cap_marks_failed(
    finalizers: dict[str, AsyncMock], recipient_ok: None
) -> None:
    job = _job(attempts=3)  # == worker_max_attempts default
    mailer = AsyncMock()
    mailer.send.side_effect = TransientSendError("timeout")

    await consumer._process_one(job, WORKER_ID, mailer)

    finalizers["mark_failed"].assert_awaited_once()
    finalizers["mark_retry"].assert_not_awaited()


async def test_lease_lost_finalizer_discards_no_resend(
    monkeypatch: pytest.MonkeyPatch, recipient_ok: None
) -> None:
    # Finalizer reports the fence was lost (0 rows) -> result discarded.
    lost = AsyncMock(return_value=False, __name__="mark_completed")
    monkeypatch.setattr(consumer, "mark_completed", lost)
    monkeypatch.setattr(consumer, "in_transaction", _fake_in_transaction(object()))
    job = _job()
    mailer = AsyncMock()

    await consumer._process_one(job, WORKER_ID, mailer)

    # Sent exactly once; no resend, no crash despite the lost lease.
    mailer.send.assert_awaited_once()
    lost.assert_awaited_once()


async def test_unexpected_error_propagates(
    finalizers: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-worker error is a programming bug: it must propagate, not retry.
    monkeypatch.setattr(
        consumer.recipients, "load", AsyncMock(side_effect=RuntimeError("bug"))
    )
    mailer = AsyncMock()

    with pytest.raises(RuntimeError, match="bug"):
        await consumer._process_one(_job(), WORKER_ID, mailer)

    finalizers["mark_failed"].assert_not_awaited()
    finalizers["mark_retry"].assert_not_awaited()


# --------------------------------------------------------------------------- #
# _drain orchestration
# --------------------------------------------------------------------------- #
class _HeartbeatStub(Heartbeat):
    """Minimal Heartbeat stand-in for unit tests — no background task, no DB."""

    def __init__(self) -> None:
        self.lease_lost: asyncio.Event = asyncio.Event()


def _heartbeat_stub() -> _HeartbeatStub:
    return _HeartbeatStub()


async def test_drain_reaper_runs_once_per_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reap = AsyncMock(return_value=0)
    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(consumer, "reap_stale", reap)
    monkeypatch.setattr(consumer, "claim_batch", claim)
    monkeypatch.setattr(consumer, "in_transaction", _fake_in_transaction(object()))

    await consumer._drain(WORKER_ID, AsyncMock(), _heartbeat_stub(), asyncio.Event())

    reap.assert_awaited_once()
    claim.assert_awaited_once()


async def test_drain_loops_on_full_batch_stops_on_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(consumer.settings, "worker_claim_batch_size", 2)
    monkeypatch.setattr(consumer, "reap_stale", AsyncMock(return_value=0))
    monkeypatch.setattr(consumer, "in_transaction", _fake_in_transaction(object()))
    monkeypatch.setattr(consumer, "_process_one", AsyncMock())
    # Full batch (2) then short batch (1) -> two claim iterations, then stop.
    claim = AsyncMock(side_effect=[[_job(), _job()], [_job()]])
    monkeypatch.setattr(consumer, "claim_batch", claim)

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(delay)

    monkeypatch.setattr(consumer.asyncio, "sleep", fake_sleep)

    await consumer._drain(WORKER_ID, AsyncMock(), _heartbeat_stub(), asyncio.Event())

    assert claim.await_count == 2
    assert cast(AsyncMock, consumer._process_one).await_count == 3
    # asyncio.sleep(0) yields between iterations (one per processed batch).
    assert sleeps == [0, 0]


async def test_drain_stops_when_lease_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(consumer, "reap_stale", AsyncMock(return_value=0))
    monkeypatch.setattr(consumer, "in_transaction", _fake_in_transaction(object()))
    claim = AsyncMock(return_value=[_job()])
    monkeypatch.setattr(consumer, "claim_batch", claim)
    hb = _heartbeat_stub()
    hb.lease_lost.set()

    await consumer._drain(WORKER_ID, AsyncMock(), hb, asyncio.Event())

    # Lease lost before the loop body -> reaper ran, but no work was claimed.
    claim.assert_not_awaited()


async def test_drain_respects_concurrency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(consumer.settings, "worker_send_concurrency", 3)
    monkeypatch.setattr(consumer.settings, "worker_claim_batch_size", 100)
    monkeypatch.setattr(consumer, "reap_stale", AsyncMock(return_value=0))
    monkeypatch.setattr(consumer, "in_transaction", _fake_in_transaction(object()))
    monkeypatch.setattr(
        consumer, "claim_batch", AsyncMock(side_effect=[[_job() for _ in range(10)]])
    )

    active = 0
    peak = 0

    async def instrumented(*_args: Any, **_kw: Any) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        # Yield repeatedly so coroutines genuinely overlap.
        for _ in range(3):
            await asyncio.sleep(0)
        active -= 1

    monkeypatch.setattr(consumer, "_process_one", instrumented)

    await consumer._drain(WORKER_ID, AsyncMock(), _heartbeat_stub(), asyncio.Event())

    assert peak <= 3
