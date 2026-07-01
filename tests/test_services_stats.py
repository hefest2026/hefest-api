"""Integration tests for hefest.services.stats.compute_organizer_stats.

Runs against the ephemeral testcontainers Postgres (``db`` fixture). All rows are
scoped to a freshly-created organizer, so the aggregates are isolated from any
other data in the shared schema. Everything created is torn down in ``finally``;
deleting the events cascades their registrations, and the users are removed last.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from hefest.models.event import Event, EventStatus
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User, UserRole
from hefest.services.stats import compute_organizer_stats

pytestmark = pytest.mark.integration


async def _user(role: UserRole) -> User:
    return await User.create(
        email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        full_name=f"Test {role}",
        role=role,
        email_verified_at=datetime.now(UTC),
    )


async def _event(
    organizer: User,
    *,
    status: EventStatus,
    capacity: int,
    starts_at: datetime,
) -> Event:
    return await Event.create(
        organizer=organizer,
        title="E",
        description="",
        starts_at=starts_at,
        location="loc",
        capacity=capacity,
        status=status,
    )


@pytest.mark.usefixtures("db")
async def test_compute_organizer_stats_aggregates_only_own_events() -> None:
    now = datetime.now(UTC)
    organizer = await _user(UserRole.organizer)
    other_org = await _user(UserRole.organizer)
    students = [await _user(UserRole.student) for _ in range(4)]
    created: list[Event | User] = [organizer, other_org, *students]
    try:
        # organizer: 1 draft, 1 past published (cap 20), 1 future published (cap 30)
        draft = await _event(
            organizer,
            status=EventStatus.draft,
            capacity=100,
            starts_at=now + timedelta(days=5),
        )
        past = await _event(
            organizer,
            status=EventStatus.published,
            capacity=20,
            starts_at=now - timedelta(days=1),
        )
        future = await _event(
            organizer,
            status=EventStatus.published,
            capacity=30,
            starts_at=now + timedelta(days=1),
        )
        # a different organizer's event with a registration — must not leak in
        foreign = await _event(
            other_org,
            status=EventStatus.published,
            capacity=50,
            starts_at=now + timedelta(days=2),
        )
        created[:0] = [draft, past, future, foreign]

        # 2 confirmed + 1 waitlisted on our events; 1 confirmed on the foreign one
        await Registration.create(
            event=past, student=students[0], status=RegistrationStatus.confirmed
        )
        recent = await Registration.create(
            event=future, student=students[1], status=RegistrationStatus.confirmed
        )
        old = await Registration.create(
            event=future, student=students[2], status=RegistrationStatus.confirmed
        )
        await Registration.create(
            event=future, student=students[3], status=RegistrationStatus.waitlisted
        )
        await Registration.create(
            event=foreign, student=students[0], status=RegistrationStatus.confirmed
        )
        # push one confirmed registration outside the 7-day window
        await Registration.filter(id=old.id).update(
            registered_at=now - timedelta(days=10)
        )

        stats = await compute_organizer_stats(organizer)

        assert stats.events_total == 3
        assert stats.events_draft == 1
        assert stats.events_published == 2
        assert stats.events_upcoming == 1  # only the future published event
        assert stats.total_capacity == 50  # 20 + 30 (published only, excludes draft)
        assert stats.total_confirmed == 3  # 3 on our events; foreign org's excluded
        assert stats.total_waitlisted == 1
        assert stats.new_registrations_7d == 2  # `past`+`recent` in window; `old` out
        assert recent.id != old.id
    finally:
        for obj in created:
            await obj.delete()


@pytest.mark.usefixtures("db")
async def test_compute_organizer_stats_zero_for_new_organizer() -> None:
    organizer = await _user(UserRole.organizer)
    try:
        stats = await compute_organizer_stats(organizer)
        assert stats.events_total == 0
        assert stats.total_capacity == 0
        assert stats.total_confirmed == 0
        assert stats.new_registrations_7d == 0
    finally:
        await organizer.delete()
