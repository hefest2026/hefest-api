"""Outbox-to-Redis relay — scaffold (HEF-16).

The relay bridges the transactional outbox (``notification_jobs``) to the Redis
stream the C++ worker consumes. It is **push-driven** via PostgreSQL
LISTEN/NOTIFY rather than a fixed-interval poll, which gives Kafka-like
end-to-end latency without adding a broker to the stack.

Mechanism
---------
1. An ``AFTER INSERT`` trigger on ``notification_jobs`` runs ``pg_notify`` on
   ``settings.relay_notify_channel``. Because NOTIFY is transactional, the
   signal is delivered exactly when the outbox row becomes visible — at the
   same COMMIT that persists the registration — so there is no dual-write race.
2. A dedicated LISTEN connection wakes the relay within milliseconds of that
   COMMIT. On wake, the relay *drains*: claims pending rows with
   ``FOR UPDATE SKIP LOCKED``, ``XADD``s them to the stream, and marks them
   ``published`` — all in one transaction.
3. NOTIFY is fire-and-forget (at-most-once): a signal emitted while the relay
   is disconnected is lost. The outbox row is still in Postgres, so a
   long-interval **fallback poll** (``settings.relay_fallback_poll_interval``)
   guarantees eventual delivery and a full catch-up drain after any downtime or
   reconnect. Push for latency, poll for durability.

Delivery is at-least-once by design: a crash between ``XADD`` and COMMIT of the
``published`` update re-publishes on the next drain. The worker deduplicates via
``notification_log`` (see spec §7.5), so duplicates are safe.

This module is a scaffold — the control flow and interfaces are fixed; the
bodies are stubbed (``NotImplementedError``) for a follow-up implementation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from tortoise import BaseDBAsyncClient

# Redis stream fields are flat; the JSON envelope the worker parses is carried
# in a single field under this key. Keep in sync with the C++ worker's parser.
MESSAGE_FIELD: str = "data"


async def claim_pending_jobs(
    conn: BaseDBAsyncClient, batch_size: int
) -> list[dict[str, Any]]:
    """Lock and return up to ``batch_size`` pending outbox rows.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent relay instances never claim
    the same row and a stuck row never blocks the batch. The lock is held until
    the enclosing transaction commits, serialising the claim with the
    ``published`` update below.

    Args:
        conn: Connection bound to the active transaction.
        batch_size: Maximum number of rows to claim.

    Returns:
        Claimed rows (``id``, ``event_type``, ``payload``, ``idempotency_key``),
        oldest first; empty when no work is pending.
    """
    # TODO(HEF-16): SELECT id, event_type, payload, idempotency_key
    #   FROM notification_jobs WHERE status = 'pending'
    #   ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED
    raise NotImplementedError


def build_envelope(row: dict[str, Any]) -> dict[str, Any]:
    """Build the worker-facing event envelope from an outbox row.

    Carries ids only — never PII. The worker loads names/emails from the DB by
    id at send time (spec §7.4).

    Args:
        row: A claimed ``notification_jobs`` row.

    Returns:
        The envelope: ``type`` + ``idempotency_key`` merged over ``payload``.
    """
    # TODO(HEF-16): {"type": row["event_type"],
    #   "idempotency_key": row["idempotency_key"], **row["payload"]}
    raise NotImplementedError


async def publish_batch(
    redis: Redis, stream: str, maxlen: int, rows: list[dict[str, Any]]
) -> None:
    """``XADD`` each row's envelope to the stream via a single pipeline.

    Publishes *before* the rows are marked ``published`` so a failure here
    leaves them ``pending`` (re-published next drain) rather than silently lost.

    Args:
        redis: Async Redis client.
        stream: Target stream key (``settings.relay_stream``).
        maxlen: Approximate ``MAXLEN ~`` cap to bound stream growth.
        rows: Rows previously claimed by :func:`claim_pending_jobs`.
    """
    # TODO(HEF-16): pipeline XADD orjson.dumps(build_envelope(row)) under
    #   MESSAGE_FIELD, with maxlen=maxlen, approximate=True; await execute().
    raise NotImplementedError


async def mark_published(conn: BaseDBAsyncClient, ids: list[Any]) -> None:
    """Mark the claimed rows ``published`` within the claiming transaction.

    Args:
        conn: The same connection that claimed the rows.
        ids: Primary keys of the published rows.
    """
    # TODO(HEF-16): UPDATE notification_jobs SET status='published',
    #   updated_at=NOW() WHERE id = ANY($1)
    raise NotImplementedError


async def drain(redis: Redis, batch_size: int, stream: str, maxlen: int) -> int:
    """Run one drain pass: claim → publish → mark, atomically.

    Returns:
        Number of rows published. A return of ``batch_size`` means the table may
        hold more pending work; the caller should drain again immediately.
    """
    # TODO(HEF-16): async with in_transaction("default") as conn:
    #   rows = await claim_pending_jobs(conn, batch_size)
    #   if not rows: return 0
    #   await publish_batch(redis, stream, maxlen, rows)
    #   await mark_published(conn, [r["id"] for r in rows])
    #   return len(rows)
    raise NotImplementedError


async def _listen(wake: asyncio.Event) -> None:
    """Maintain the LISTEN connection; set ``wake`` on every NOTIFY.

    Owns a dedicated raw asyncpg connection (never a pooled one — pooled
    connections are recycled and lose LISTEN registration). Reconnects with
    backoff on drop and sets ``wake`` on each successful (re)connect to force a
    catch-up drain for any NOTIFY missed while disconnected.

    Args:
        wake: Event shared with :func:`_drainer`; set to request a drain.
    """
    # TODO(HEF-16): loop { conn = await asyncpg.connect(dsn);
    #   await conn.add_listener(channel, lambda *_: wake.set());
    #   wake.set();  # catch up on (re)connect
    #   await <termination>; } with reconnect backoff. dsn = db_url with the
    #   asyncpg:// scheme rewritten to postgresql:// for asyncpg.connect().
    raise NotImplementedError


async def _drainer(redis: Redis, wake: asyncio.Event) -> None:
    """Drain whenever woken by NOTIFY, or every fallback interval as a backstop.

    Drains fully on entry (startup catch-up), then waits on ``wake`` with a
    timeout equal to the fallback poll interval. Either trigger drains the table
    to empty before waiting again.

    Args:
        redis: Async Redis client.
        wake: Event set by :func:`_listen` (and on fallback timeout).
    """
    # TODO(HEF-16): while running:
    #   while await drain(...) == batch_size: pass   # drain to empty
    #   wake.clear()
    #   with suppress(TimeoutError):
    #       await asyncio.wait_for(wake.wait(), settings.relay_fallback_poll_interval)
    raise NotImplementedError


async def run() -> None:
    """Wire dependencies and run the listener + drainer until shutdown.

    Initialises Tortoise and Redis, starts :func:`_listen` and :func:`_drainer`
    on a shared :class:`asyncio.Event`, and tears everything down on signal.
    """
    # TODO(HEF-16): RegisterTortoise/Tortoise.init; redis = from_url(...);
    #   wake = asyncio.Event(); gather(_listen(wake), _drainer(redis, wake));
    #   install SIGTERM/SIGINT handlers; close redis + Tortoise on exit.
    raise NotImplementedError


def main() -> None:
    """Process entrypoint: ``python -m hefest.worker.relay``."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
