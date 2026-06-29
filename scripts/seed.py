"""Seed fixture accounts for development and production.

Creates (or resets) two well-known accounts:

    student@adviz.bg   / Student!123   — role: student
    organizer@adviz.bg / Organizer!123 — role: organizer

Safe to run multiple times: existing accounts are updated in place so their
password and verification status are always in sync with this file.

Usage:
    PYTHONPATH=. uv run python scripts/seed.py            # connect via HEFEST_DB_URL
    PYTHONPATH=. uv run python scripts/seed.py --dry-run  # dry run, no writes
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from typing import TypedDict

from tortoise import Tortoise

from hefest.config import TORTOISE_ORM
from hefest.models.user import User, UserRole
from hefest.services.auth import hash_password


class SeedAccount(TypedDict):
    """Typed structure for a seed account definition."""

    email: str
    password: str
    full_name: str
    role: UserRole


SEED_ACCOUNTS: list[SeedAccount] = [
    {
        "email": "student@adviz.bg",
        "password": "Student!123",
        "full_name": "Тестов Студент",
        "role": UserRole.student,
    },
    {
        "email": "organizer@adviz.bg",
        "password": "Organizer!123",
        "full_name": "Тестов Организатор",
        "role": UserRole.organizer,
    },
]


async def seed(*, dry_run: bool) -> None:
    """Upsert all seed accounts.

    Args:
        dry_run: When True, print planned actions without writing to the database.
    """
    await Tortoise.init(config=TORTOISE_ORM)

    now = datetime.now(UTC)

    for account in SEED_ACCOUNTS:
        email: str = account["email"]
        role: UserRole = account["role"]
        full_name: str = account["full_name"]
        password_hash = hash_password(account["password"])

        existing = await User.get_or_none(email=email)

        if dry_run:
            action = "UPDATE" if existing else "CREATE"
            print(f"[dry-run] {action}  {role:10s}  {email}")
            continue

        if existing:
            existing.password_hash = password_hash
            existing.full_name = full_name
            existing.role = role
            existing.email_verified_at = now
            await existing.save(
                update_fields=[
                    "password_hash",
                    "full_name",
                    "role",
                    "email_verified_at",
                ]
            )
            print(f"UPDATED  {role:10s}  {email}")
        else:
            await User.create(
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                role=role,
                email_verified_at=now,
            )
            print(f"CREATED  {role:10s}  {email}")

    await Tortoise.close_connections()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be seeded without writing to the database.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(seed(dry_run=args.dry_run))
