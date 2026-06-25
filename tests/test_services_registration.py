"""Unit tests for hefest.services.registration.

All Tortoise ORM calls and the transaction context are mocked so no database
is required.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from tortoise.exceptions import IntegrityError

from hefest.models.event import EventStatus
from hefest.models.registration import RegistrationStatus
from hefest.models.user import UserRole
from hefest.services import registration as svc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(role: UserRole = UserRole.student) -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    u.role = role
    return u


def _event(
    capacity: int = 10,
    status: EventStatus = EventStatus.published,
    starts_at: datetime | None = None,
    title: str = "Test Event",
) -> MagicMock:
    e = MagicMock()
    e.id = uuid.uuid4()
    e.organizer_id = uuid.uuid4()
    e.capacity = capacity
    e.status = status
    e.title = title
    e.starts_at = starts_at or datetime(2026, 9, 1, tzinfo=UTC)
    return e


def _registration(
    student_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    reg_status: RegistrationStatus = RegistrationStatus.confirmed,
    registered_at: datetime | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.student_id = student_id or uuid.uuid4()
    r.event_id = event_id or uuid.uuid4()
    r.status = reg_status
    r.registered_at = registered_at or datetime(2026, 6, 1, tzinfo=UTC)
    r.cancelled_at = None
    r.save = AsyncMock()
    return r


@asynccontextmanager
async def _null_tx() -> Any:
    """No-op async context manager that replaces in_transaction()."""
    yield None


def _mock_tx() -> MagicMock:
    m = MagicMock()
    m.return_value = _null_tx()
    return m


# ---------------------------------------------------------------------------
# Helper: mock a Tortoise queryset chain
#
# Pattern:  Model.filter(...).using_db(conn).select_for_update().get_or_none()
#           Model.filter(...).using_db(conn).count()
#           Model.filter(...).using_db(conn).order_by(...).first()
# ---------------------------------------------------------------------------


def _qs(*, get: Any = None, count: int = 0, first: Any = None) -> MagicMock:
    """Build a mock queryset that supports common chaining methods."""
    qs = MagicMock()
    qs.using_db.return_value = qs
    qs.select_for_update.return_value = qs
    qs.order_by.return_value = qs
    qs.filter.return_value = qs
    qs.get_or_none = AsyncMock(return_value=get)
    qs.count = AsyncMock(return_value=count)
    qs.first = AsyncMock(return_value=first)
    qs.all = AsyncMock(return_value=[])
    return qs


# ---------------------------------------------------------------------------
# register_student
# ---------------------------------------------------------------------------


class TestRegisterStudent:
    async def test_confirmed_when_capacity_available(self) -> None:
        student = _user()
        evt = _event(capacity=10)
        event_id = evt.id
        reg = _registration(student_id=student.id, event_id=event_id)

        event_qs = _qs(get=evt)
        confirmed_qs = _qs(count=5)
        job_create = AsyncMock()
        reg_create = AsyncMock(return_value=reg)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=event_qs),
            patch.object(
                svc.Registration,
                "filter",
                return_value=confirmed_qs,
            ),
            patch.object(svc.Registration, "create", new=reg_create),
            patch.object(svc.NotificationJob, "create", new=job_create),
        ):
            result = await svc.register_student(student, event_id)

        assert result.status == RegistrationStatus.confirmed
        assert result.waitlist_position is None
        _, job_kwargs = job_create.call_args
        assert job_kwargs["event_type"] == "RegistrationConfirmed"

    async def test_waitlisted_when_at_capacity(self) -> None:
        student = _user()
        evt = _event(capacity=3)
        event_id = evt.id
        reg = _registration(
            student_id=student.id,
            event_id=event_id,
            reg_status=RegistrationStatus.waitlisted,
        )

        # confirmed_count == 3 (at capacity); waitlist count == 2 already queued
        call_count = 0

        def _filter_side_effect(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Event.filter for the event row
                return _qs(get=evt)
            if call_count == 2:
                # Registration.filter for confirmed count
                return _qs(count=3)
            # Registration.filter for waitlist count
            return _qs(count=2)

        job_create = AsyncMock()
        reg_create = AsyncMock(return_value=reg)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", side_effect=_filter_side_effect),
            patch.object(svc.Registration, "filter", side_effect=_filter_side_effect),
            patch.object(svc.Registration, "create", new=reg_create),
            patch.object(svc.NotificationJob, "create", new=job_create),
        ):
            # Reset call_count per class — patch both independently
            pass

        # Re-run with cleaner, explicit mock setup
        evt_qs = _qs(get=evt)
        confirmed_qs = _qs(count=3)
        waitlist_qs = _qs(count=2)

        filter_calls: list[MagicMock] = [evt_qs, confirmed_qs, waitlist_qs]
        idx = 0

        def _pick_qs(*_args: Any, **_kwargs: Any) -> MagicMock:
            nonlocal idx
            qs = filter_calls[min(idx, len(filter_calls) - 1)]
            idx += 1
            return qs

        job_create2 = AsyncMock()
        reg_create2 = AsyncMock(return_value=reg)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", side_effect=_pick_qs),
            patch.object(svc.Registration, "filter", side_effect=_pick_qs),
            patch.object(svc.Registration, "create", new=reg_create2),
            patch.object(svc.NotificationJob, "create", new=job_create2),
        ):
            result = await svc.register_student(student, event_id)

        assert result.status == RegistrationStatus.waitlisted
        assert result.waitlist_position == 3  # 2 ahead + 1
        _, job_kwargs = job_create2.call_args
        assert job_kwargs["event_type"] == "RegistrationWaitlisted"

    async def test_event_not_found_raises_404(self) -> None:
        student = _user()
        event_qs = _qs(get=None)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=event_qs),
        ):
            with pytest.raises(HTTPException) as exc:
                await svc.register_student(student, uuid.uuid4())

        assert exc.value.status_code == 404

    async def test_draft_event_raises_404(self) -> None:
        student = _user()
        evt = _event(status=EventStatus.draft)
        event_qs = _qs(get=evt)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=event_qs),
        ):
            with pytest.raises(HTTPException) as exc:
                await svc.register_student(student, evt.id)

        assert exc.value.status_code == 404

    async def test_duplicate_registration_raises_409(self) -> None:
        student = _user()
        evt = _event(capacity=10)
        event_qs = _qs(get=evt)
        confirmed_qs = _qs(count=0)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=event_qs),
            patch.object(svc.Registration, "filter", return_value=confirmed_qs),
            patch.object(
                svc.Registration, "create", new=AsyncMock(side_effect=IntegrityError)
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await svc.register_student(student, evt.id)

        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# cancel_registration
# ---------------------------------------------------------------------------


class TestCancelRegistration:
    async def test_confirmed_cancellation_promotes_next_waitlisted(self) -> None:
        student = _user()
        event_id = uuid.uuid4()
        reg = _registration(
            student_id=student.id,
            event_id=event_id,
            reg_status=RegistrationStatus.confirmed,
        )
        evt = _event(starts_at=datetime.now(UTC) + timedelta(days=1))
        evt.id = event_id
        next_wl = _registration(
            event_id=event_id,
            reg_status=RegistrationStatus.waitlisted,
        )
        next_wl.student_id = uuid.uuid4()

        reg_qs = _qs(get=reg)
        event_qs = _qs(get=evt)
        waitlist_qs = _qs(first=next_wl)
        job_create = AsyncMock()

        idx = 0
        order = [reg_qs, event_qs, waitlist_qs]

        def _pick(*_a: Any, **_kw: Any) -> MagicMock:
            nonlocal idx
            qs = order[min(idx, len(order) - 1)]
            idx += 1
            return qs

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Registration, "filter", side_effect=_pick),
            patch.object(svc.Event, "filter", side_effect=_pick),
            patch.object(svc.NotificationJob, "create", new=job_create),
        ):
            result = await svc.cancel_registration(student, reg.id)

        assert result.status == RegistrationStatus.cancelled

        # Two outbox jobs: WaitlistPromoted + RegistrationCancelled
        assert job_create.await_count == 2
        job_types = {call.kwargs["event_type"] for call in job_create.call_args_list}
        assert "WaitlistPromoted" in job_types
        assert "RegistrationCancelled" in job_types

        # Next waitlisted is promoted
        assert next_wl.status == RegistrationStatus.confirmed
        next_wl.save.assert_awaited_once()

    async def test_waitlisted_cancellation_no_promotion(self) -> None:
        student = _user()
        event_id = uuid.uuid4()
        reg = _registration(
            student_id=student.id,
            event_id=event_id,
            reg_status=RegistrationStatus.waitlisted,
        )
        evt = _event(starts_at=datetime.now(UTC) + timedelta(days=1))
        evt.id = event_id

        reg_qs = _qs(get=reg)
        event_qs = _qs(get=evt)
        # No further waitlisted to promote — but the query shouldn't run at all
        job_create = AsyncMock()

        order = [reg_qs, event_qs]
        idx = 0

        def _pick(*_a: Any, **_kw: Any) -> MagicMock:
            nonlocal idx
            qs = order[min(idx, len(order) - 1)]
            idx += 1
            return qs

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Registration, "filter", side_effect=_pick),
            patch.object(svc.Event, "filter", side_effect=_pick),
            patch.object(svc.NotificationJob, "create", new=job_create),
        ):
            await svc.cancel_registration(student, reg.id)

        # Only RegistrationCancelled — no WaitlistPromoted
        assert job_create.await_count == 1
        assert job_create.call_args.kwargs["event_type"] == "RegistrationCancelled"

    async def test_already_cancelled_raises_409(self) -> None:
        student = _user()
        reg = _registration(
            student_id=student.id,
            reg_status=RegistrationStatus.cancelled,
        )
        reg_qs = _qs(get=reg)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Registration, "filter", return_value=reg_qs),
        ):
            with pytest.raises(HTTPException) as exc:
                await svc.cancel_registration(student, reg.id)

        assert exc.value.status_code == 409

    async def test_not_found_raises_404(self) -> None:
        student = _user()
        reg_qs = _qs(get=None)

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Registration, "filter", return_value=reg_qs),
        ):
            with pytest.raises(HTTPException) as exc:
                await svc.cancel_registration(student, uuid.uuid4())

        assert exc.value.status_code == 404

    async def test_event_started_raises_409(self) -> None:
        student = _user()
        event_id = uuid.uuid4()
        reg = _registration(
            student_id=student.id,
            event_id=event_id,
            reg_status=RegistrationStatus.confirmed,
        )
        past = datetime(2020, 1, 1, tzinfo=UTC)
        evt = _event(starts_at=past)
        evt.id = event_id

        reg_qs = _qs(get=reg)
        event_qs = _qs(get=evt)
        order = [reg_qs, event_qs]
        idx = 0

        def _pick(*_a: Any, **_kw: Any) -> MagicMock:
            nonlocal idx
            qs = order[min(idx, len(order) - 1)]
            idx += 1
            return qs

        with (
            patch("hefest.services.registration.in_transaction", _mock_tx()),
            patch.object(svc.Registration, "filter", side_effect=_pick),
            patch.object(svc.Event, "filter", side_effect=_pick),
        ):
            with pytest.raises(HTTPException) as exc:
                await svc.cancel_registration(student, reg.id)

        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# list_my_registrations
# ---------------------------------------------------------------------------


class TestListMyRegistrations:
    async def test_waitlist_position_computed(self) -> None:
        student = _user()
        event_id = uuid.uuid4()
        reg = _registration(
            student_id=student.id,
            event_id=event_id,
            reg_status=RegistrationStatus.waitlisted,
            registered_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        reg.event_id = event_id

        # 1 student is ahead in the waitlist
        ahead_qs = _qs(count=1)
        ahead_qs.all = AsyncMock(return_value=[reg])

        main_qs = MagicMock()
        main_qs.all = AsyncMock(return_value=[reg])

        def _filter(*_a: Any, **_kw: Any) -> MagicMock:
            return ahead_qs

        with (
            patch.object(svc.Registration, "filter", side_effect=[main_qs, ahead_qs]),
        ):
            main_qs.all = AsyncMock(return_value=[reg])
            ahead_qs.count = AsyncMock(return_value=1)
            result = await svc.list_my_registrations(student)

        assert len(result) == 1
        assert result[0].waitlist_position == 2  # 1 ahead + 1

    async def test_confirmed_has_no_position(self) -> None:
        student = _user()
        reg = _registration(
            student_id=student.id,
            reg_status=RegistrationStatus.confirmed,
        )

        main_qs = MagicMock()
        main_qs.all = AsyncMock(return_value=[reg])

        with patch.object(svc.Registration, "filter", return_value=main_qs):
            result = await svc.list_my_registrations(student)

        assert result[0].waitlist_position is None
