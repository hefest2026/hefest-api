"""CI migration dry-run: render every migration's SQL without a database.

Uses :func:`tortoise.migrations.api.sqlmigrate`, which initialises Tortoise with
``init_connections=False`` and a no-op recorder, so it generates the forward SQL
for each migration purely from the migration files and the live models — no
PostgreSQL instance required. A malformed migration, a model/migration mismatch
that breaks SQL generation, or a missing migration package fails the build.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from tortoise import Tortoise
from tortoise.migrations.api.sqlmigrate import sqlmigrate

from hefest.config import TORTOISE_ORM


async def _dry_run() -> int:
    """Render forward SQL for every migration of every configured app.

    Returns:
        Process exit code: 0 when all migrations render, 1 on any problem.
    """
    apps: dict[str, dict[str, str]] = TORTOISE_ORM.get("apps", {})
    if not apps:
        print("No apps configured in TORTOISE_ORM")
        return 1

    rendered = 0
    for app_label, app_config in apps.items():
        migrations_dir = Path(app_config.get("migrations", "migrations"))
        if not migrations_dir.is_dir():
            print(f"Migrations directory missing for {app_label!r}: {migrations_dir}")
            return 1

        migration_names = sorted(
            path.stem for path in migrations_dir.glob("*.py") if path.stem != "__init__"
        )
        if not migration_names:
            print(f"No migrations found for app {app_label!r} in {migrations_dir}")
            return 1

        for name in migration_names:
            statements = await sqlmigrate(
                config=TORTOISE_ORM,
                app_label=app_label,
                migration_name=name,
            )
            print(f"--- {app_label}.{name} ---")
            print("\n".join(statements))
            rendered += 1
            with contextlib.suppress(Exception):
                await Tortoise.close_connections()

    print(f"\nOK: dry-ran {rendered} migration(s) without executing any SQL")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_dry_run()))
