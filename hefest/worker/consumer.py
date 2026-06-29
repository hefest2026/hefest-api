"""Outbox consumer — the worker's integration core (HEF-39, spec §3-§6).

This module wires every prior worker module into a single running loop. It is
push-driven via PostgreSQL LISTEN/NOTIFY (the same machinery as the legacy
``relay``) with a fallback poll for durability, and it owns the strict
transaction boundaries the data-integrity guarantee depends on.

The single most important invariant (spec §3)
----------------------------------------------
The claim runs in a brief transaction that COMMITS and releases the connection
**before** any SMTP send. Each finalizer (``mark_completed`` / ``mark_retry`` /
``mark_failed``) then runs in its own separate short transaction. A transaction
is NEVER held open across a send. Every finalizer is fenced and returns a
``bool``: ``False`` means the lease was reclaimed mid-send, so the result is
DISCARDED — never resent, never restamped.

Delivery is **at-least-once**: the send happens outside any transaction, so a
crash or DB outage between a successful send and its finalizer leaves the job
``processing`` for the reaper to retry, resending the email. ``_commit_finalizer``
retries transient DB failures to shrink this window, but consumers must tolerate
rare duplicates.

Liveness self-abort (spec §4, §6.2)
-----------------------------------
The consumer stops claiming new work the moment ``heartbeat.lease_lost`` is set.
``__main__`` then exits non-zero so the orchestrator restarts a clean process —
"a worker that cannot prove it is alive must assume it is dead."

Testability
-----------
:func:`_drain` and :func:`_process_one` are module-level functions so the drain
orchestration and the per-job decision matrix can be unit-tested directly with
mocks. The LISTEN/NOTIFY + signal loop in :func:`run` is exercised by the Task 10
integration tests.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

import asyncpg
from tortoise.exceptions import DBConnectionError, OperationalError
from tortoise.transactions import in_transaction

from hefest.config import settings
from hefest.worker import recipients, templates
from hefest.worker.claim import (
    backoff_delay,
    claim_batch,
    mark_completed,
    mark_failed,
    mark_retry,
    reap_stale,
)
from hefest.worker.errors import PermanentError, TransientError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hefest.worker.claim import ClaimedJob
    from hefest.worker.heartbeat import Heartbeat
    from hefest.worker.mailer import Mailer

logger = logging.getLogger(__name__)

# Reconnect backoff bounds (seconds) for the dedicated LISTEN connection.
_RECONNECT_BACKOFF_INITIAL: float = 1.0
_RECONNECT_BACKOFF_MAX: float = 30.0
# Cadence (seconds) of the keep-alive ping on the LISTEN connection. asyncpg
# dispatches NOTIFY callbacks from its background reader, so this loop only keeps
# the socket alive and surfaces a silently dropped connection as a raised error.
_LISTEN_HEARTBEAT_SECONDS: float = 30.0

# Finalizer commit retry: a successful send has already happened by the time a
# finalizer runs, so a brief DB blip (failover, connection drop, timeout) on the
# state write must not abandon the job in `processing` — the reaper would later
# resend the email. Retry transient connection/operational errors a few times
# with short backoff; logical errors and an exhausted budget propagate.
_FINALIZE_MAX_ATTEMPTS: int = 3
_FINALIZE_RETRY_BASE_SECONDS: float = 0.5
# Transient DB failures worth retrying. Logical errors (IntegrityError, etc.)
# are deliberately excluded. asyncpg connection errors surface either directly
# or wrapped by Tortoise as DBConnectionError/OperationalError; OSError covers
# socket-level drops (and TimeoutError, its subclass).
_TRANSIENT_DB_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    DBConnectionError,
    OperationalError,
    asyncpg.PostgresConnectionError,
)


async def _finalize(
    finalizer: Callable[..., Awaitable[bool]],
    job: ClaimedJob,
    worker_id: str,
    **kwargs: object,
) -> None:
    """Run one fenced finalizer in its OWN short transaction (spec §3).

    Opens a dedicated transaction, applies the terminal state, and — if the
    fence reports the lease was reclaimed mid-send (``False`` = 0 rows) — logs
    and discards the result. It never resends or restamps a lost lease.

    The commit is retried across transient DB failures (see
    :func:`_commit_finalizer`) so a brief outage between a successful send and
    the state write does not strand the job for the reaper to resend.

    Args:
        finalizer: A fenced finalizer (``mark_completed``/``mark_retry``/
            ``mark_failed``) taking ``(conn, job_id, worker_id, **kwargs)``.
        job: The job being finalized.
        worker_id: This worker's fencing token; must still own the lease.
        **kwargs: Extra finalizer arguments (e.g. ``last_error``,
            ``delay_seconds``).
    """
    held = await _commit_finalizer(finalizer, job, worker_id, **kwargs)
    if not held:
        logger.warning(
            "Lease lost finalizing job %s via %s; discarding result",
            job.id,
            getattr(finalizer, "__name__", finalizer),
        )


async def _commit_finalizer(
    finalizer: Callable[..., Awaitable[bool]],
    job: ClaimedJob,
    worker_id: str,
    **kwargs: object,
) -> bool:
    """Run the finalizer transaction, retrying transient DB failures.

    The terminal write is fenced and idempotent: on a retry where a prior
    attempt actually committed, the finalizer matches 0 rows and returns
    ``False`` (logged as a discarded result), so retrying can never double-apply
    or resend.

    Args:
        finalizer: The fenced finalizer to run.
        job: The job being finalized.
        worker_id: This worker's fencing token.
        **kwargs: Extra finalizer arguments.

    Returns:
        The finalizer's fence result (``True`` applied, ``False`` lease lost).

    Raises:
        Exception: The last transient DB error once the retry budget is
            exhausted, or any non-transient error immediately.
    """
    for attempt in range(1, _FINALIZE_MAX_ATTEMPTS + 1):
        try:
            async with in_transaction("default") as conn:
                return await finalizer(conn, job.id, worker_id, **kwargs)
        except _TRANSIENT_DB_ERRORS as exc:
            if attempt == _FINALIZE_MAX_ATTEMPTS:
                logger.error(
                    "Transient DB error finalizing job %s via %s; exhausted %d "
                    "attempts — job stays claimed and may resend after reaping: "
                    "%s",
                    job.id,
                    getattr(finalizer, "__name__", finalizer),
                    _FINALIZE_MAX_ATTEMPTS,
                    exc,
                )
                raise
            delay = _FINALIZE_RETRY_BASE_SECONDS * attempt
            logger.warning(
                "Transient DB error finalizing job %s (attempt %d/%d); retrying "
                "in %.1fs: %s",
                job.id,
                attempt,
                _FINALIZE_MAX_ATTEMPTS,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises


async def _process_one(job: ClaimedJob, worker_id: str, mailer: Mailer) -> None:
    """Process one claimed job through the per-job decision matrix (spec §4).

    Loads the recipient, renders the email, and sends it. The send happens with
    NO transaction open (the claim already committed). Outcomes route to exactly
    one fenced finalizer, each in its own transaction:

    * success → ``mark_completed``
    * ``PermanentError`` (bad recipient, unknown type, 5xx) → ``mark_failed``
    * ``TransientError`` with attempts exhausted → ``mark_failed``
    * ``TransientError`` otherwise → ``mark_retry`` with exponential backoff

    ``asyncio.CancelledError`` and any non-worker exception propagate — only
    ``TransientError``/``PermanentError`` are handled here, so a programming
    error is never silently turned into a retry.

    Args:
        job: The claimed job to deliver.
        worker_id: This worker's fencing token.
        mailer: The kept-alive SMTP mailer.
    """
    try:
        recipient = await recipients.load(job.payload)
        content = templates.render(
            job.event_type, recipient.user, recipient.event, job.payload
        )
        await mailer.send(content, recipient.user.email)
    except PermanentError as exc:
        await _finalize(mark_failed, job, worker_id, last_error=str(exc))
        return
    except TransientError as exc:
        if job.attempts >= settings.worker_max_attempts:
            await _finalize(mark_failed, job, worker_id, last_error=str(exc))
        else:
            delay = backoff_delay(job.attempts, settings.worker_backoff_base_seconds)
            await _finalize(
                mark_retry, job, worker_id, last_error=str(exc), delay_seconds=delay
            )
        return
    await _finalize(mark_completed, job, worker_id)


async def _bounded_process(
    semaphore: asyncio.Semaphore, job: ClaimedJob, worker_id: str, mailer: Mailer
) -> None:
    """Process one job while holding a send-concurrency slot.

    Args:
        semaphore: Caps concurrent in-flight sends to
            ``settings.worker_send_concurrency``.
        job: The claimed job to deliver.
        worker_id: This worker's fencing token.
        mailer: The kept-alive SMTP mailer.
    """
    async with semaphore:
        await _process_one(job, worker_id, mailer)


async def _drain(
    worker_id: str, mailer: Mailer, heartbeat: Heartbeat, stop: asyncio.Event
) -> None:
    """Drain the outbox to empty: one reaper pass, then claim → process (spec §4).

    Runs the reaper once per wake, then repeatedly claims a batch (each claim in
    its own committed transaction released BEFORE any send) and processes it
    concurrently under a send-concurrency semaphore. Yields control with
    ``asyncio.sleep(0)`` between iterations so a hot queue never starves the
    heartbeat task (spec §6.2). Returns when the queue is drained (short/empty
    batch) or when ``stop``/``lease_lost`` is set — after a lost lease it claims
    no new work.

    Args:
        worker_id: This worker's fencing token.
        mailer: The kept-alive SMTP mailer.
        heartbeat: The lease heartbeat; its ``lease_lost`` event halts claiming.
        stop: Shutdown signal; halts claiming when set.
    """
    async with in_transaction("default") as conn:
        reaped = await reap_stale(conn, settings.worker_reaper_idle_seconds)
    if reaped:
        logger.info("Reaper reclaimed %d stale lease(s)", reaped)

    batch_size = settings.worker_claim_batch_size
    semaphore = asyncio.Semaphore(settings.worker_send_concurrency)
    while True:
        if heartbeat.lease_lost.is_set() or stop.is_set():
            return

        # Claim in its own committed transaction, released BEFORE any send: the
        # transaction-boundary guarantee (spec §3). No transaction is open while
        # the batch is processed below.
        async with in_transaction("default") as conn:
            jobs = await claim_batch(conn, worker_id, batch_size)

        if not jobs:
            return

        # TaskGroup (not bare gather): if one job raises an unexpected error,
        # its siblings are cancelled and awaited before the group propagates,
        # leaving no in-flight sends running on the loop. Bare gather would let
        # them survive — harmless under a restarting container, but a source of
        # cross-test interference and teardown races. Expected per-job failures
        # never reach here (they are handled inside _process_one).
        async with asyncio.TaskGroup() as tg:
            for job in jobs:
                tg.create_task(_bounded_process(semaphore, job, worker_id, mailer))

        # Yield so a saturated queue never starves the heartbeat task (spec §6.2).
        await asyncio.sleep(0)

        if len(jobs) < batch_size:
            return


async def _listen(wake: asyncio.Event) -> None:
    """Maintain the LISTEN connection; set ``wake`` on every NOTIFY (ported).

    Owns a dedicated raw asyncpg connection (never a pooled one — pooled
    connections are recycled and lose LISTEN registration). Reconnects with
    backoff on drop and sets ``wake`` on each successful (re)connect to force a
    catch-up drain for any NOTIFY missed while disconnected.

    Args:
        wake: Event shared with :func:`run`; set to request a drain.
    """
    # asyncpg.connect wants a libpq-style URL; settings.db_url carries Tortoise's
    # asyncpg:// scheme.
    dsn = settings.db_url.replace("asyncpg://", "postgresql://", 1)
    channel = settings.worker_notify_channel
    backoff = _RECONNECT_BACKOFF_INITIAL
    while True:
        try:
            conn = await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning(
                "Worker LISTEN connect failed (%s); retrying in %ss", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
            continue

        backoff = _RECONNECT_BACKOFF_INITIAL
        try:
            await conn.add_listener(channel, lambda *_: wake.set())
            # Force a catch-up drain for any NOTIFY missed while disconnected.
            wake.set()
            logger.info("Worker listening on '%s' for outbox notifications", channel)
            # Keep the socket alive and surface a dropped connection as a raised
            # error; NOTIFY callbacks fire on asyncpg's background reader.
            while True:
                await asyncio.sleep(_LISTEN_HEARTBEAT_SECONDS)
                await conn.execute("SELECT 1")
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning("Worker LISTEN connection lost (%s); reconnecting", exc)
        finally:
            with suppress(OSError, asyncpg.PostgresError):
                await conn.close()


async def run(
    worker_id: str, mailer: Mailer, heartbeat: Heartbeat, stop: asyncio.Event
) -> None:
    """Consume the outbox until shutdown: LISTEN/NOTIFY + fallback poll (spec §4).

    Starts a dedicated LISTEN task, drains fully on entry (startup catch-up),
    then waits on the ``wake`` event with a timeout of
    ``settings.worker_fallback_poll_interval`` — either trigger drives another
    drain. Exits the loop once ``stop`` or ``heartbeat.lease_lost`` is set, and
    always tears down its LISTEN task. ``__main__`` constructs and owns
    ``mailer``, ``heartbeat``, and ``stop``.

    Args:
        worker_id: This worker's fencing token (``host:uuid``).
        mailer: The kept-alive SMTP mailer.
        heartbeat: The lease heartbeat; ``lease_lost`` ends consumption.
        stop: Shutdown signal set by ``__main__``'s signal handlers.
    """
    wake = asyncio.Event()
    listen_task = asyncio.create_task(_listen(wake))
    interval = settings.worker_fallback_poll_interval
    try:
        while not (stop.is_set() or heartbeat.lease_lost.is_set()):
            # Clear before draining: a NOTIFY arriving during the drain re-sets
            # the event so the wait below returns immediately and drains again,
            # rather than the signal being lost between drain and clear.
            wake.clear()
            await _drain(worker_id, mailer, heartbeat, stop)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), interval)
    finally:
        listen_task.cancel()
        with suppress(asyncio.CancelledError):
            await listen_task
