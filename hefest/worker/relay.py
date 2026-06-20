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

"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import asyncpg
import orjson
import redis.asyncio as aioredis
from loguru import logger
from tortoise import Tortoise
from tortoise.transactions import in_transaction

from hefest.config import TORTOISE_ORM, settings
from hefest.logging import configure_logging

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from tortoise import BaseDBAsyncClient

# Redis stream fields are flat; the JSON envelope the worker parses is carried
# in a single field under this key. Keep in sync with the C++ worker's parser.
MESSAGE_FIELD: str = "data"

# Reconnect backoff bounds (seconds) for the dedicated LISTEN connection.
_RECONNECT_BACKOFF_INITIAL: float = 1.0
_RECONNECT_BACKOFF_MAX: float = 30.0
# Heartbeat cadence (seconds) on the LISTEN connection. asyncpg dispatches
# NOTIFY callbacks from its background reader, so this loop only has to keep the
# socket alive and surface a silently dropped connection as a raised error.
_LISTEN_HEARTBEAT_SECONDS: float = 30.0


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
    rows = await conn.execute_query_dict(
        """
        SELECT id, event_type, payload, idempotency_key
        FROM notification_jobs
        WHERE status = 'pending'
        ORDER BY created_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
        """,
        [batch_size],
    )
    # asyncpg returns jsonb as raw text on raw queries (no ORM decode layer);
    # normalise to a dict so build_envelope can splat it.
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, (str, bytes, bytearray)):
            row["payload"] = orjson.loads(payload)
    return rows


def build_envelope(row: dict[str, Any]) -> dict[str, Any]:
    """Build the worker-facing event envelope from an outbox row.

    Carries ids only — never PII. The worker loads names/emails from the DB by
    id at send time (spec §7.4).

    Args:
        row: A claimed ``notification_jobs`` row.

    Returns:
        The envelope: ``type`` + ``idempotency_key`` merged over ``payload``.
    """
    # Contract fields are spread last so a stray "type"/"idempotency_key" in the
    # stored payload can never shadow the relay's authoritative envelope keys.
    return {
        **row["payload"],
        "type": row["event_type"],
        "idempotency_key": row["idempotency_key"],
    }


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
    async with redis.pipeline(transaction=False) as pipe:
        for row in rows:
            pipe.xadd(
                stream,
                {MESSAGE_FIELD: orjson.dumps(build_envelope(row))},
                maxlen=maxlen,
                approximate=True,
            )
        await pipe.execute()


async def mark_published(conn: BaseDBAsyncClient, ids: list[Any]) -> None:
    """Mark the claimed rows ``published`` within the claiming transaction.

    Args:
        conn: The same connection that claimed the rows.
        ids: Primary keys of the published rows.
    """
    await conn.execute_query(
        """
        UPDATE notification_jobs
        SET status = 'published', updated_at = NOW()
        WHERE id = ANY($1::uuid[])
        """,
        [ids],
    )


async def drain(redis: Redis, batch_size: int, stream: str, maxlen: int) -> int:
    """Run one drain pass: claim → publish → mark, atomically.

    Returns:
        Number of rows published. A return of ``batch_size`` means the table may
        hold more pending work; the caller should drain again immediately.
    """
    async with in_transaction("default") as conn:
        rows = await claim_pending_jobs(conn, batch_size)
        if not rows:
            return 0
        # Publish before marking published: a failure here leaves the rows
        # pending (re-published next drain) rather than silently dropped. The
        # row lock is held until COMMIT, so the published update is serialised
        # with the claim.
        await publish_batch(redis, stream, maxlen, rows)
        await mark_published(conn, [row["id"] for row in rows])
        return len(rows)


async def _listen(wake: asyncio.Event) -> None:
    """Maintain the LISTEN connection; set ``wake`` on every NOTIFY.

    Owns a dedicated raw asyncpg connection (never a pooled one — pooled
    connections are recycled and lose LISTEN registration). Reconnects with
    backoff on drop and sets ``wake`` on each successful (re)connect to force a
    catch-up drain for any NOTIFY missed while disconnected.

    Args:
        wake: Event shared with :func:`_drainer`; set to request a drain.
    """
    # asyncpg.connect wants a libpq-style URL; settings.db_url carries Tortoise's
    # asyncpg:// scheme.
    dsn = settings.db_url.replace("asyncpg://", "postgresql://", 1)
    channel = settings.relay_notify_channel
    backoff = _RECONNECT_BACKOFF_INITIAL
    while True:
        try:
            conn = await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning(
                "Relay LISTEN connect failed ({}); retrying in {}s", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
            continue

        backoff = _RECONNECT_BACKOFF_INITIAL
        try:
            await conn.add_listener(channel, lambda *_: wake.set())
            # Force a catch-up drain for any NOTIFY missed while disconnected.
            wake.set()
            logger.info("Relay listening on '{}' for outbox notifications", channel)
            # Keep the socket alive and surface a dropped connection as a raised
            # error; NOTIFY callbacks fire on asyncpg's background reader.
            while True:
                await asyncio.sleep(_LISTEN_HEARTBEAT_SECONDS)
                await conn.execute("SELECT 1")
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning("Relay LISTEN connection lost ({}); reconnecting", exc)
        finally:
            with suppress(Exception):
                await conn.close()


async def _drainer(redis: Redis, wake: asyncio.Event) -> None:
    """Drain whenever woken by NOTIFY, or every fallback interval as a backstop.

    Drains fully on entry (startup catch-up), then waits on ``wake`` with a
    timeout equal to the fallback poll interval. Either trigger drains the table
    to empty before waiting again.

    Args:
        redis: Async Redis client.
        wake: Event set by :func:`_listen` (and on fallback timeout).
    """
    batch_size = settings.relay_batch_size
    stream = settings.relay_stream
    maxlen = settings.relay_stream_maxlen
    interval = settings.relay_fallback_poll_interval
    while True:
        # Clear before draining: a NOTIFY arriving during the drain re-sets the
        # event so the wait below returns immediately and we drain again, rather
        # than the signal being lost in the gap between drain and clear.
        wake.clear()
        # A full batch means more rows may be pending — keep draining to empty.
        while await drain(redis, batch_size, stream, maxlen) == batch_size:
            pass
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake.wait(), interval)


async def run() -> None:
    """Wire dependencies and run the listener + drainer until shutdown.

    Initialises Tortoise and Redis, starts :func:`_listen` and :func:`_drainer`
    on a shared :class:`asyncio.Event`, and tears everything down on signal.
    """
    configure_logging(settings)
    await Tortoise.init(config=TORTOISE_ORM)
    redis: Redis = aioredis.from_url(settings.redis_url)

    wake = asyncio.Event()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    listen_task = asyncio.create_task(_listen(wake))
    drain_task = asyncio.create_task(_drainer(redis, wake))
    logger.info("Relay started (stream='{}')", settings.relay_stream)
    try:
        await stop.wait()
    finally:
        logger.info("Relay shutting down")
        for task in (listen_task, drain_task):
            task.cancel()
        await asyncio.gather(listen_task, drain_task, return_exceptions=True)
        await redis.aclose()
        await Tortoise.close_connections()


def main() -> None:
    """Process entrypoint: ``python -m hefest.worker.relay``."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
