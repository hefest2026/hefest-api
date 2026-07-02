"""Notification worker process entrypoint — ``python -m hefest.worker`` (HEF-39).

Owns the worker's lifecycle and wiring (spec §6): initialise logging and the
worker's own (larger) Tortoise pool, mint a unique ``host:uuid`` fencing token,
construct the mailer and heartbeat, and run the heartbeat and consumer as
concurrent tasks until any of them finishes or a shutdown signal arrives.

Shutdown contract
-----------------
On SIGTERM/SIGINT (graceful) or ``heartbeat.lease_lost`` (self-abort) the
consumer task is cancelled — cancelling its in-flight ``gather`` and SMTP sends
— alongside the heartbeat task, then the mailer and DB connections are closed.
If the cause was a lost lease the process exits NON-ZERO so the orchestrator
(``restart: unless-stopped``) restarts a clean process. A normal signal exits 0.
"A worker that cannot prove it is alive must assume it is dead."
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import sys
import uuid

from tortoise import Tortoise

from hefest.config import build_worker_tortoise_orm, settings
from hefest.logging import configure_logging
from hefest.worker import consumer
from hefest.worker.heartbeat import Heartbeat
from hefest.worker.mailer import Mailer

logger = logging.getLogger(__name__)


async def _run() -> None:
    """Initialise dependencies, run heartbeat + consumer, and shut down cleanly.

    Raises:
        SystemExit: With code 1 if the heartbeat lost its lease (self-abort), so
            the orchestrator restarts a clean process.
    """
    configure_logging(settings)
    await Tortoise.init(config=build_worker_tortoise_orm())

    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    mailer = Mailer(settings)
    heartbeat = Heartbeat(
        worker_id,
        interval=settings.worker_heartbeat_interval,
        reaper_idle=settings.worker_reaper_idle_seconds,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    heartbeat_task = asyncio.create_task(heartbeat.run())
    consumer_task = asyncio.create_task(
        consumer.run(worker_id, mailer, heartbeat, stop)
    )
    stop_waiter = asyncio.create_task(stop.wait())
    lease_waiter = asyncio.create_task(heartbeat.lease_lost.wait())
    waiters = (heartbeat_task, consumer_task, stop_waiter, lease_waiter)

    logger.info("Worker %s started", worker_id)
    try:
        # Wake on the first of: graceful stop, lease loss, or a task ending.
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        lease_lost = heartbeat.lease_lost.is_set()
        logger.info("Worker %s shutting down (lease_lost=%s)", worker_id, lease_lost)
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        await mailer.aclose()
        await Tortoise.close_connections()

    if lease_lost:
        logger.error("Worker %s exiting non-zero: lease lost", worker_id)
        sys.exit(1)


def main() -> None:
    """Process entrypoint: ``python -m hefest.worker``."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
