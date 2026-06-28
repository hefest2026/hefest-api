"""Unit tests for the lease-renewal heartbeat + self-abort (HEF-39, spec §6.2).

The loop is driven without real time: ``asyncio.sleep`` is a no-op (or a counter
that raises ``CancelledError`` to end a healthy run), the module's ``monotonic``
clock is a controllable counter, and ``in_transaction`` yields a mock connection.
Assertions target real behavior — the ``lease_lost`` state transitions and the
renewal SQL — not mock internals. The central guarantee under test (spec §6.2):
a single failed renewal must not self-abort; only failures spanning more than
``reaper_idle`` since the last success do, and a recovery resets the clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hefest.worker import heartbeat

WORKER_ID = "worker-1"
INTERVAL = 90.0
REAPER_IDLE = 300.0


def _counter(values: Iterable[float]) -> Callable[[], float]:
    """Return a zero-arg clock that yields ``values`` in order."""
    it = iter(values)
    return lambda: next(it)


class _FakeTx:
    """Async-context-manager stand-in for ``in_transaction(...)``."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_in_transaction(conn: object) -> Callable[[str], _FakeTx]:
    """Build an ``in_transaction`` replacement yielding ``conn``."""
    return lambda _name: _FakeTx(conn)


def _sleep_until(limit: int) -> Callable[[float], Any]:
    """Async ``sleep`` that raises ``CancelledError`` after ``limit`` calls."""
    state = {"n": 0}

    async def fake_sleep(_delay: float) -> None:
        state["n"] += 1
        if state["n"] > limit:
            raise asyncio.CancelledError

    return fake_sleep


# --- renew_heartbeat SQL ---------------------------------------------------


async def test_renew_heartbeat_sql_and_count() -> None:
    conn = AsyncMock()
    conn.execute_query.return_value = (4, [])

    renewed = await heartbeat.renew_heartbeat(conn, WORKER_ID)

    query, values = conn.execute_query.call_args.args
    assert "heartbeat_at = statement_timestamp()" in query
    assert "updated_at = statement_timestamp()" in query
    assert "WHERE locked_by = $1 AND status = 'processing'" in query
    assert values == [WORKER_ID]
    assert renewed == 4


# --- run loop --------------------------------------------------------------


async def test_healthy_renews_and_keeps_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hb = heartbeat.Heartbeat(WORKER_ID, interval=INTERVAL, reaper_idle=REAPER_IDLE)
    renew = AsyncMock(return_value=2)
    monkeypatch.setattr(heartbeat, "renew_heartbeat", renew)
    monkeypatch.setattr(heartbeat, "in_transaction", _fake_in_transaction(AsyncMock()))
    monkeypatch.setattr(heartbeat, "monotonic", _counter([0, 90, 180, 270]))
    monkeypatch.setattr(asyncio, "sleep", _sleep_until(3))

    with pytest.raises(asyncio.CancelledError):
        await hb.run()

    assert renew.await_count == 3
    assert not hb.lease_lost.is_set()


async def test_self_abort_after_reaper_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hb = heartbeat.Heartbeat(WORKER_ID, interval=INTERVAL, reaper_idle=REAPER_IDLE)
    renew = AsyncMock(side_effect=OSError("db down"))
    monkeypatch.setattr(heartbeat, "renew_heartbeat", renew)
    monkeypatch.setattr(heartbeat, "in_transaction", _fake_in_transaction(AsyncMock()))
    # start=0; failures observed at 90, 180, 270 (within window), 360 (> 300).
    monkeypatch.setattr(heartbeat, "monotonic", _counter([0, 90, 180, 270, 360]))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await hb.run()

    assert hb.lease_lost.is_set()
    assert renew.await_count == 4


async def test_transient_blip_recovers_no_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hb = heartbeat.Heartbeat(WORKER_ID, interval=INTERVAL, reaper_idle=REAPER_IDLE)
    # one blip well within the window, then two clean renewals.
    renew = AsyncMock(side_effect=[OSError("blip"), 1, 1])
    monkeypatch.setattr(heartbeat, "renew_heartbeat", renew)
    monkeypatch.setattr(heartbeat, "in_transaction", _fake_in_transaction(AsyncMock()))
    monkeypatch.setattr(heartbeat, "monotonic", _counter([0, 90, 180, 270]))
    monkeypatch.setattr(asyncio, "sleep", _sleep_until(3))

    with pytest.raises(asyncio.CancelledError):
        await hb.run()

    assert not hb.lease_lost.is_set()
    assert renew.await_count == 3
