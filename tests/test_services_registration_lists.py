"""Integration tests for organizer-facing registration/waitlist listings.

Covers ``hefest.services.registration.list_event_registrations`` against a live
Postgres (``db`` fixture): the confirmed list and the FIFO waitlist must each
carry the student's resolved name/email, respect deterministic ordering, and
enforce event ownership.

Isolation follows the repo convention: every row is created with fresh UUIDs and
the owning ``User`` rows are deleted in teardown, with FK ``ON DELETE CASCADE``
removing derived events/registrations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from hefest.models.event import Event, EventStatus
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User, UserRole
from hefest.services.registration import list_event_registrations

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("db")]


def _unique_email(prefix: str) -> str:
    return f"{prefix}+{uuid.uuid4().hex[:8]}@listtest.hefest.local"


async def _make_student(name: str) -> User:
    return await User.create(
        email=_unique_email("stu"),
        full_name=name,
        role=UserRole.student,
    )


async def _register(
    student: User,
    event: Event,
    status: RegistrationStatus,
    registered_at: datetime,
) -> Registration:
    """Create a registration then pin ``registered_at`` to a controlled value.

    ``registered_at`` is ``auto_now_add`` so it cannot be set on create; the
    explicit update makes FIFO ordering deterministic instead of relying on
    sub-millisecond insertion timing.
    """
    reg = await Registration.create(student=student, event=event, status=status)
    await Registration.filter(id=reg.id).update(registered_at=registered_at)
    return reg


async def test_confirmed_list_carries_names_and_emails() -> None:
    organizer = await User.create(
        email=_unique_email("org"), full_name="List Organizer", role=UserRole.organizer
    )
    try:
        event = await Event.create(
            organizer=organizer,
            title="List Test",
            description="safe to delete",
            starts_at=datetime.now(UTC) + timedelta(days=3),
            location="Hall B",
            capacity=10,
            status=EventStatus.published,
        )
        alice = await _make_student("Alice Anderson")
        bob = await _make_student("Bob Brown")
        base = datetime.now(UTC)
        await _register(
            bob, event, RegistrationStatus.confirmed, base + timedelta(minutes=1)
        )
        await _register(alice, event, RegistrationStatus.confirmed, base)

        result = await list_event_registrations(organizer, event.id)

        # Oldest-first ordering: Alice (base) precedes Bob (base + 1 min).
        assert [r.student_name for r in result] == ["Alice Anderson", "Bob Brown"]
        assert result[0].student_email == alice.email
        assert result[1].student_id == bob.id
        assert all(r.status == RegistrationStatus.confirmed for r in result)
    finally:
        await User.filter(id__in=[organizer.id, alice.id, bob.id]).delete()


async def test_waitlist_returns_fifo_order_and_excludes_confirmed() -> None:
    organizer = await User.create(
        email=_unique_email("org"), full_name="WL Organizer", role=UserRole.organizer
    )
    students: list[User] = []
    try:
        event = await Event.create(
            organizer=organizer,
            title="Waitlist Test",
            description="safe to delete",
            starts_at=datetime.now(UTC) + timedelta(days=3),
            location="Hall C",
            capacity=1,
            status=EventStatus.published,
        )
        base = datetime.now(UTC)
        confirmed = await _make_student("Confirmed Carl")
        students.append(confirmed)
        await _register(confirmed, event, RegistrationStatus.confirmed, base)
        for i, name in enumerate(("Wait One", "Wait Two", "Wait Three")):
            student = await _make_student(name)
            students.append(student)
            await _register(
                student,
                event,
                RegistrationStatus.waitlisted,
                base + timedelta(minutes=i + 1),
            )

        waitlist = await list_event_registrations(
            organizer, event.id, waitlist_only=True
        )

        assert [r.student_name for r in waitlist] == [
            "Wait One",
            "Wait Two",
            "Wait Three",
        ]
        assert all(r.status == RegistrationStatus.waitlisted for r in waitlist)
    finally:
        await User.filter(id__in=[organizer.id, *[s.id for s in students]]).delete()


async def test_foreign_organizer_gets_404() -> None:
    owner = await User.create(
        email=_unique_email("org"), full_name="Owner", role=UserRole.organizer
    )
    intruder = await User.create(
        email=_unique_email("org"), full_name="Intruder", role=UserRole.organizer
    )
    try:
        event = await Event.create(
            organizer=owner,
            title="Owned Event",
            description="safe to delete",
            starts_at=datetime.now(UTC) + timedelta(days=3),
            location="Hall D",
            capacity=5,
            status=EventStatus.published,
        )
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await list_event_registrations(intruder, event.id)
        assert exc.value.status_code == 404
    finally:
        await User.filter(id__in=[owner.id, intruder.id]).delete()
