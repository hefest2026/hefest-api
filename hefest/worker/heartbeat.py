"""Lease-renewal heartbeat with self-abort (HEF-39, spec §4, §5, §6.2).

A healthy worker must keep its claimed (``processing``) rows fresh so the reaper
(:func:`hefest.worker.claim.reap_stale`) never reclaims them out from under it —
including rows still queued behind the send semaphore, not just the one in
flight. Every ``interval`` seconds this background task renews ``heartbeat_at``
for **all** rows it owns (``WHERE locked_by = $self AND status = 'processing'``).

The dual guarantee (spec §4): the fence (``locked_by``) protects each ROW; this
heartbeat is the liveness backstop. If the worker cannot reach the database for
longer than ``reaper_idle`` seconds, every lease it held has provably been
reaped, so it **self-aborts** by setting :attr:`Heartbeat.lease_lost`. The
consumer (Task 7) watches that event to cancel in-flight sends and ``__main__``
exits non-zero for a clean orchestrator restart. "A worker that cannot prove it
is alive must assume it is dead."

The critical subtlety (spec §6.2): a *single* failed renewal must NOT trip the
self-abort. Only a run of failures spanning more than ``reaper_idle`` seconds
since the last success does — two missed beats (interval 90, reaper_idle 300)
are tolerated within the window, and any successful renewal resets the staleness
clock. ``monotonic`` is referenced as a module attribute so tests can drive the
loop without real time.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING

import asyncpg
from tortoise.exceptions import DBConnectionError, OperationalError
from tortoise.transactions import in_transaction

if TYPE_CHECKING:
    from tortoise import BaseDBAsyncClient

logger = logging.getLogger(__name__)

# Database/connection failures that mark a renewal as unreachable. A failure here
# does not immediately abort — only a run of them past ``reaper_idle`` does.
_RENEW_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    asyncpg.PostgresError,
    DBConnectionError,
    OperationalError,
)


async def renew_heartbeat(conn: BaseDBAsyncClient, worker_id: str) -> int:
    """Renew ``heartbeat_at`` for every ``processing`` row this worker holds.

    Args:
        conn: Connection bound to the caller's short renewal transaction.
        worker_id: This worker's fencing token (``locked_by``).

    Returns:
        The number of rows renewed.
    """
    affected, _ = await conn.execute_query(
        """
        UPDATE notification_jobs
        SET heartbeat_at = statement_timestamp(),
            updated_at = statement_timestamp()
        WHERE locked_by = $1 AND status = 'processing'
        """,
        [worker_id],
    )
    return affected


class Heartbeat:
    """Background lease-renewal task with a liveness self-abort.

    Attributes:
        lease_lost: Set once the worker decides it has provably lost its leases
            (renewals failed continuously for longer than ``reaper_idle``). The
            consumer and ``__main__`` watch this event.
    """

    def __init__(self, worker_id: str, *, interval: float, reaper_idle: float) -> None:
        """Initialize the heartbeat.

        Args:
            worker_id: This worker's fencing token (``locked_by``).
            interval: Seconds between renewal attempts.
            reaper_idle: Staleness threshold; once renewals have failed for
                longer than this since the last success, the worker self-aborts.
        """
        self._worker_id = worker_id
        self._interval = interval
        self._reaper_idle = reaper_idle
        self.lease_lost: asyncio.Event = asyncio.Event()

    async def run(self) -> None:
        """Renew leases every ``interval`` until cancelled or self-aborted.

        Loops indefinitely renewing this worker's leases. On a database/
        connection failure it logs a warning and keeps trying; only when the
        time since the last successful renewal exceeds ``reaper_idle`` does it
        set :attr:`lease_lost` and return. ``asyncio.CancelledError`` propagates
        for clean shutdown.
        """
        last_success = monotonic()
        while True:
            await asyncio.sleep(self._interval)
            try:
                async with in_transaction("default") as conn:
                    await renew_heartbeat(conn, self._worker_id)
                last_success = monotonic()
            except _RENEW_ERRORS as exc:
                stale_for = monotonic() - last_success
                logger.warning(
                    "Heartbeat renewal failed for worker %s (stale %.1fs): %s",
                    self._worker_id,
                    stale_for,
                    exc,
                )
                if stale_for > self._reaper_idle:
                    logger.error(
                        "Heartbeat self-aborting worker %s: leases provably "
                        "lost (no successful renewal for %.1fs > %.1fs)",
                        self._worker_id,
                        stale_for,
                        self._reaper_idle,
                    )
                    self.lease_lost.set()
                    return
