"""Session-scoped ephemeral Postgres for DB-backed integration tests.

Spins up a throwaway ``postgres:16-alpine`` container via testcontainers, points
``settings.db_url`` at it, and creates the schema once per session with
``Tortoise.generate_schemas`` (migration *correctness* is covered separately by
the ``migrations`` CI job / ``scripts/ci_check_migrations.py``). The container is
the test run's own database process — no shared dev DB and no manual migration
step, so these tests actually execute in CI instead of skipping.

Tests opt in by depending on the ``db`` fixture. When Docker is unavailable
(a local run with no daemon, or ``act`` without a mounted socket) the dependent
tests skip with a clear reason rather than failing — the only honest skip, since
without a container there is genuinely no database to run against.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from docker.errors import DockerException
from tortoise import Tortoise

from hefest.config import build_worker_tortoise_orm, settings


async def _create_schema() -> None:
    """Create the full model schema once on a throwaway connection."""
    await Tortoise.init(config=build_worker_tortoise_orm())
    try:
        await Tortoise.generate_schemas()
    finally:
        await Tortoise.close_connections()


@pytest.fixture(scope="session")
def pg_container() -> Iterator[None]:
    """Start an ephemeral Postgres for the session; repoint ``settings.db_url``.

    A docker-unreachable failure (no daemon / no socket) skips the dependent
    tests; any other failure propagates so a real container problem is not
    silently masked.
    """
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer(
            "postgres:16-alpine",
            username="hefest",
            password="hefest",
            dbname="hefest_db",
        )
        container.start()
    except (DockerException, OSError) as exc:
        pytest.skip(f"Docker unavailable for integration DB: {exc}")

    original_db_url = settings.db_url
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    settings.db_url = f"asyncpg://hefest:hefest@{host}:{port}/hefest_db"
    try:
        asyncio.run(_create_schema())
        yield
    finally:
        settings.db_url = original_db_url
        container.stop()


@pytest.fixture()
async def db(pg_container: None) -> AsyncIterator[None]:
    """Initialise Tortoise against the session container; close on teardown.

    Init/close is per-test (on the test's own event loop) while the container
    and schema are shared for the whole session.
    """
    await Tortoise.init(config=build_worker_tortoise_orm())
    try:
        yield
    finally:
        await Tortoise.close_connections()
