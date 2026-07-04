"""Unit tests for hefest.services.event.

All Tortoise ORM calls are mocked so no database is required.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from hefest.models.event import EventStatus
from hefest.models.notification import NotificationType
from hefest.models.registration import RegistrationStatus
from hefest.models.user import UserRole
from hefest.schemas.event import EventCreateRequest, EventUpdateRequest
from hefest.services import event as svc

UTC = UTC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _null_tx() -> Any:
    """No-op async context manager that replaces in_transaction()."""
    yield None


def _mock_tx() -> MagicMock:
    m = MagicMock()
    m.side_effect = lambda: _null_tx()
    return m


def _event_qs(get: Any = None) -> MagicMock:
    """Mock Event.filter chain: .using_db().select_for_update().get_or_none()."""
    qs = MagicMock()
    qs.using_db.return_value = qs
    qs.select_for_update.return_value = qs
    qs.get_or_none = AsyncMock(return_value=get)
    return qs


def _reg_list_qs(items: list[Any] | None = None) -> MagicMock:
    """Mock Registration.filter chain awaited as a list.

    Models the pattern: ``await Registration.filter(...).using_db(conn)``
    where ``using_db`` is an AsyncMock so that awaiting its call returns the
    list directly (calling an AsyncMock returns a coroutine; awaiting that
    coroutine yields the ``return_value``).
    """
    qs = MagicMock()
    qs.using_db = AsyncMock(return_value=items or [])
    return qs


def _user(role: UserRole = UserRole.organizer, user_id: str | None = None) -> MagicMock:
    u = MagicMock()
    u.id = uuid.UUID(user_id) if user_id else uuid.uuid4()
    u.role = role
    return u


def _event(
    organizer_id: uuid.UUID | None = None,
    status: EventStatus = EventStatus.draft,
    starts_at: datetime | None = None,
) -> MagicMock:
    e = MagicMock()
    e.id = uuid.uuid4()
    e.organizer_id = organizer_id or uuid.uuid4()
    e.status = status
    e.title = "Test Event"
    e.description = ""
    e.starts_at = starts_at or datetime(2026, 9, 1, tzinfo=UTC)
    e.ends_at = None
    e.location = "Hall A"
    e.capacity = 50
    e.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    e.updated_at = datetime(2026, 6, 1, tzinfo=UTC)
    return e


def _create_request(**kwargs: Any) -> EventCreateRequest:
    defaults: dict[str, Any] = {
        "title": "Science Fair",
        "description": "Annual fair",
        "starts_at": datetime(2026, 9, 1, tzinfo=UTC),
        "ends_at": None,
        "location": "Hall A",
        "capacity": 100,
    }
    return EventCreateRequest(**(defaults | kwargs))


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


class TestCreateEvent:
    async def test_creates_draft(self) -> None:
        organizer = _user(UserRole.organizer)
        data = _create_request()

        mock_event = _event(organizer_id=organizer.id)
        mock_create = AsyncMock(return_value=mock_event)
        with patch.object(svc.Event, "create", new=mock_create):
            result = await svc.create_event(organizer, data)

        assert result is mock_event
        mock_create.assert_awaited_once()
        _, kwargs = mock_create.call_args
        assert kwargs["status"] == EventStatus.draft
        assert kwargs["organizer"] is organizer


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


class TestListEvents:
    def _paginated_qs(self, items: list[Any]) -> MagicMock:
        qs = MagicMock()
        qs.annotate.return_value = qs
        qs.order_by.return_value = qs
        qs.offset.return_value = qs
        qs.limit = AsyncMock(return_value=items)
        return qs

    async def test_student_sees_only_published(self) -> None:
        student = _user(UserRole.student)
        published = [_event(status=EventStatus.published)]

        with patch.object(
            svc.Event, "filter", return_value=self._paginated_qs(published)
        ) as mock_filter:
            result = await svc.list_events(student)

        mock_filter.assert_called_once_with(status=EventStatus.published)
        assert result == published

    async def test_organizer_sees_own_and_published(self) -> None:
        organizer = _user(UserRole.organizer)
        events = [
            _event(organizer_id=organizer.id),
            _event(status=EventStatus.published),
        ]

        with patch.object(svc.Event, "filter", return_value=self._paginated_qs(events)):
            result = await svc.list_events(organizer)

        assert result == events


# ---------------------------------------------------------------------------
# publish_event
# ---------------------------------------------------------------------------


class TestPublishEvent:
    async def test_draft_becomes_published(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.save = AsyncMock()

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            result = await svc.publish_event(organizer, evt.id)

        assert result.status == EventStatus.published
        evt.save.assert_awaited_once_with(update_fields=["status", "updated_at"])

    async def test_not_found_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await svc.publish_event(organizer, uuid.uuid4())

        assert exc.value.status_code == 404

    async def test_wrong_owner_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=uuid.uuid4(), status=EventStatus.draft)

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            with pytest.raises(HTTPException) as exc:
                await svc.publish_event(organizer, evt.id)

        assert exc.value.status_code == 404

    async def test_already_published_raises_409(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            with pytest.raises(HTTPException) as exc:
                await svc.publish_event(organizer, evt.id)

        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# cancel_event
# ---------------------------------------------------------------------------


def _reg(
    event_id: uuid.UUID | None = None,
    status: RegistrationStatus = RegistrationStatus.confirmed,
) -> MagicMock:
    """Build a mock Registration row."""
    r = MagicMock()
    r.id = uuid.uuid4()
    r.student_id = uuid.uuid4()
    r.event_id = event_id or uuid.uuid4()
    r.status = status
    return r


def _event_update_qs(evt: Any) -> MagicMock:
    """Mock Event.filter chain: .using_db().select_for_update().get_or_none()."""
    qs = MagicMock()
    qs.using_db.return_value = qs
    qs.select_for_update.return_value = qs
    qs.get_or_none = AsyncMock(return_value=evt)
    return qs


class TestCancelEvent:
    """Tests for cancel_event — covers status transitions and fan-out jobs."""

    def _patches(
        self,
        evt: Any,
        regs: list[Any] | None = None,
    ) -> tuple[Any, Any, Any, Any, Any]:
        """Return context managers for the standard cancel_event mock set."""
        return (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_qs(get=evt)),
            patch.object(
                svc.Registration,
                "filter",
                return_value=_reg_list_qs(items=regs or []),
            ),
            patch.object(svc.NotificationJob, "bulk_create", new=AsyncMock()),
            patch.object(svc.Notification, "bulk_create", new=AsyncMock()),
        )

    async def test_draft_event_gets_cancelled(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.save = AsyncMock()

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5:
            result = await svc.cancel_event(organizer, evt.id)

        assert result.status == EventStatus.cancelled
        evt.save.assert_awaited_once()

    async def test_already_cancelled_is_idempotent(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.cancelled)
        evt.save = AsyncMock()

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        p1, p2, p3, _, _ = self._patches(evt)
        with (
            p1,
            p2,
            p3,
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            result = await svc.cancel_event(organizer, evt.id)

        assert result.status == EventStatus.cancelled
        evt.save.assert_not_awaited()
        job_bulk.assert_not_awaited()
        note_bulk.assert_not_awaited()

    async def test_wrong_owner_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=uuid.uuid4(), status=EventStatus.published)

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5, pytest.raises(HTTPException) as exc:
            await svc.cancel_event(organizer, evt.id)

        assert exc.value.status_code == 404

    async def test_not_found_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)

        p1, p2, p3, p4, p5 = self._patches(None)
        with p1, p2, p3, p4, p5, pytest.raises(HTTPException) as exc:
            await svc.cancel_event(organizer, uuid.uuid4())

        assert exc.value.status_code == 404

    async def test_past_event_raises_409(self) -> None:
        organizer = _user(UserRole.organizer)
        past = datetime(2020, 1, 1, tzinfo=UTC)
        evt = _event(
            organizer_id=organizer.id, status=EventStatus.published, starts_at=past
        )
        evt.save = AsyncMock()

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5, pytest.raises(HTTPException) as exc:
            await svc.cancel_event(organizer, evt.id)

        assert exc.value.status_code == 409

    async def test_fan_out_enqueues_confirmed_and_waitlisted(self) -> None:
        """Cancelling enqueues one EventCancelled job AND notification per reg.

        Confirmed and waitlisted registrations are included. Exclusion of
        cancelled registrations is verified via the DB-filter assertion in
        test_fan_out_excludes_cancelled_registrations.
        """
        organizer = _user(UserRole.organizer)
        event_id = uuid.uuid4()
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)
        evt.id = event_id
        evt.save = AsyncMock()

        regs = [
            _reg(event_id=event_id, status=RegistrationStatus.confirmed),
            _reg(event_id=event_id, status=RegistrationStatus.confirmed),
            _reg(event_id=event_id, status=RegistrationStatus.waitlisted),
        ]

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        with (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_qs(get=evt)),
            patch.object(
                svc.Registration,
                "filter",
                return_value=_reg_list_qs(items=regs),
            ),
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            result = await svc.cancel_event(organizer, event_id)

        assert result.status == EventStatus.cancelled
        job_bulk.assert_awaited_once()
        note_bulk.assert_awaited_once()
        jobs: list[Any] = job_bulk.call_args[0][0]
        notes: list[Any] = note_bulk.call_args[0][0]
        assert len(jobs) == 3
        assert len(notes) == 3

        # Build independent source sets from the regs this test created so that
        # a shared bug (e.g. using event_id instead of reg.id) cannot mask itself.
        source_reg_ids = {str(r.id) for r in regs}
        source_student_ids = {str(r.student_id) for r in regs}
        for job in jobs:
            assert job.event_type == "EventCancelled"
            rid = job.payload["registration_id"]
            assert rid in source_reg_ids
            assert job.idempotency_key == f"{rid}:EventCancelled"
            assert job.payload["student_id"] in source_student_ids
            assert job.payload["event_id"] == str(event_id)
            assert "occurred_at" in job.payload
            assert "user_id" not in job.payload

        # Every notification mirrors a job: same type, recipient, and event.
        # (FK ``_id`` attrs aren't exposed on unsaved instances, so the payload
        # — which carries the same recipient/event — is the assertion surface.)
        for note in notes:
            assert note.notification_type == NotificationType.event_cancelled
            assert note.payload["student_id"] in source_student_ids
            assert note.payload["event_id"] == str(event_id)
        assert {n.payload["student_id"] for n in notes} == source_student_ids

    async def test_fan_out_excludes_cancelled_registrations(self) -> None:
        """The service queries only confirmed+waitlisted; cancelled regs are out.

        Verifies the DB filter uses the correct status__in argument. Job field
        content (idempotency_key, payload shape) is verified in
        test_fan_out_enqueues_confirmed_and_waitlisted.
        """
        organizer = _user(UserRole.organizer)
        event_id = uuid.uuid4()
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)
        evt.id = event_id
        evt.save = AsyncMock()

        # Simulate the service returning only 2 active regs (cancelled already
        # filtered at DB level); also have a cancelled reg that must NOT appear.
        active_regs = [
            _reg(event_id=event_id, status=RegistrationStatus.confirmed),
            _reg(event_id=event_id, status=RegistrationStatus.waitlisted),
        ]

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        reg_filter_mock = _reg_list_qs(items=active_regs)
        reg_filter_spy = MagicMock(return_value=reg_filter_mock)
        with (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_qs(get=evt)),
            patch.object(svc.Registration, "filter", reg_filter_spy),
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            await svc.cancel_event(organizer, event_id)

        # Verify the filter was called with the correct statuses.
        call_kwargs = reg_filter_spy.call_args[1]
        assert set(call_kwargs["status__in"]) == {
            RegistrationStatus.confirmed,
            RegistrationStatus.waitlisted,
        }
        # Exactly 2 jobs + 2 notifications — the cancelled reg was excluded.
        assert len(job_bulk.call_args[0][0]) == 2
        assert len(note_bulk.call_args[0][0]) == 2

    async def test_fan_out_no_jobs_when_no_registrations(self) -> None:
        """Cancelling an event with zero active registrations skips bulk_create."""
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)
        evt.save = AsyncMock()

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        with (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_qs(get=evt)),
            patch.object(
                svc.Registration, "filter", return_value=_reg_list_qs(items=[])
            ),
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            await svc.cancel_event(organizer, evt.id)

        job_bulk.assert_not_awaited()
        note_bulk.assert_not_awaited()

    async def test_already_cancelled_enqueues_zero_jobs(self) -> None:
        """Re-cancelling an already-cancelled event enqueues no new jobs."""
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.cancelled)
        evt.save = AsyncMock()

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        with (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_qs(get=evt)),
            patch.object(svc.Registration, "filter", return_value=_reg_list_qs()),
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            result = await svc.cancel_event(organizer, evt.id)

        assert result.status == EventStatus.cancelled
        job_bulk.assert_not_awaited()
        note_bulk.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_event
# ---------------------------------------------------------------------------


class TestUpdateEvent:
    def _patches(
        self, evt: Any, regs: list[Any] | None = None
    ) -> tuple[Any, Any, Any, Any, Any]:
        """Return context managers for the standard update_event mock set.

        ``regs`` defaults to empty so the EventUpdated fan-out short-circuits;
        tests exercising the fan-out pass explicit registrations.
        """
        return (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_update_qs(evt)),
            patch.object(
                svc.Registration, "filter", return_value=_reg_list_qs(items=regs or [])
            ),
            patch.object(svc.NotificationJob, "bulk_create", new=AsyncMock()),
            patch.object(svc.Notification, "bulk_create", new=AsyncMock()),
        )

    async def test_partial_update_applies_fields(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        data = EventUpdateRequest(title="New Title", capacity=200)

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5:
            await svc.update_event(organizer, evt.id, data)

        called_dict = evt.update_from_dict.call_args[0][0]
        assert called_dict == {"title": "New Title", "capacity": 200}

    async def test_update_on_published_is_allowed(self) -> None:
        """Updates are no longer restricted to DRAFT events."""
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        data = EventUpdateRequest(title="Updated")

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5:
            await svc.update_event(organizer, evt.id, data)

        evt.update_from_dict.assert_called_once()

    async def test_wrong_owner_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=uuid.uuid4(), status=EventStatus.draft)

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5, pytest.raises(HTTPException) as exc:
            await svc.update_event(organizer, evt.id, EventUpdateRequest(title="x"))

        assert exc.value.status_code == 404

    async def test_location_locked_within_2h_raises_409(self) -> None:
        organizer = _user(UserRole.organizer)
        soon = datetime.now(UTC) + timedelta(minutes=30)
        evt = _event(
            organizer_id=organizer.id, status=EventStatus.published, starts_at=soon
        )
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        data = EventUpdateRequest(location="New Location")

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5, pytest.raises(HTTPException) as exc:
            await svc.update_event(organizer, evt.id, data)

        assert exc.value.status_code == 409

    async def test_ends_at_can_be_cleared_to_none(self) -> None:
        """Explicitly sending ends_at=null clears it (nullable field)."""
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.ends_at = datetime(2026, 9, 2, tzinfo=UTC)
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        data = EventUpdateRequest.model_validate({"ends_at": None})
        # model_fields_set must contain "ends_at" for the clear to take effect
        assert "ends_at" in data.model_fields_set

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5:
            await svc.update_event(organizer, evt.id, data)

        called_dict = evt.update_from_dict.call_args[0][0]
        assert "ends_at" in called_dict
        assert called_dict["ends_at"] is None

    async def test_omitted_fields_not_updated(self) -> None:
        """Fields absent from the request body are not touched."""
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        data = EventUpdateRequest(title="Only Title")

        p1, p2, p3, p4, p5 = self._patches(evt)
        with p1, p2, p3, p4, p5:
            await svc.update_event(organizer, evt.id, data)

        called_dict = evt.update_from_dict.call_args[0][0]
        assert list(called_dict.keys()) == ["title"]

    async def test_visible_field_change_fans_out_updated(self) -> None:
        """A user-visible field change enqueues EventUpdated jobs + notifications."""
        organizer = _user(UserRole.organizer)
        event_id = uuid.uuid4()
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)
        evt.id = event_id
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        regs = [
            _reg(event_id=event_id, status=RegistrationStatus.confirmed),
            _reg(event_id=event_id, status=RegistrationStatus.waitlisted),
        ]

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        with (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_update_qs(evt)),
            patch.object(
                svc.Registration, "filter", return_value=_reg_list_qs(items=regs)
            ),
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            await svc.update_event(
                organizer, event_id, EventUpdateRequest(title="Renamed")
            )

        job_bulk.assert_awaited_once()
        note_bulk.assert_awaited_once()
        jobs = job_bulk.call_args[0][0]
        notes = note_bulk.call_args[0][0]
        assert len(jobs) == 2
        assert len(notes) == 2
        source_student_ids = {str(r.student_id) for r in regs}
        for job in jobs:
            assert job.event_type == "EventUpdated"
            assert job.payload["event_id"] == str(event_id)
            assert job.payload["student_id"] in source_student_ids
            # occurred_at makes repeated edits idempotency-key unique
            assert job.idempotency_key.startswith(
                f"{job.payload['registration_id']}:EventUpdated:"
            )
        for note in notes:
            assert note.notification_type == NotificationType.event_updated
            assert note.payload["event_id"] == str(event_id)
        assert {n.payload["student_id"] for n in notes} == source_student_ids

    async def test_capacity_only_change_stays_silent(self) -> None:
        """A capacity-only edit notifies nobody (no user-visible field changed)."""
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.published)
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        job_bulk = AsyncMock()
        note_bulk = AsyncMock()
        reg_filter_spy = MagicMock(return_value=_reg_list_qs(items=[]))
        with (
            patch("hefest.services.event.in_transaction", _mock_tx()),
            patch.object(svc.Event, "filter", return_value=_event_update_qs(evt)),
            patch.object(svc.Registration, "filter", reg_filter_spy),
            patch.object(svc.NotificationJob, "bulk_create", new=job_bulk),
            patch.object(svc.Notification, "bulk_create", new=note_bulk),
        ):
            await svc.update_event(organizer, evt.id, EventUpdateRequest(capacity=300))

        # No registrations were even queried, and nothing was enqueued.
        reg_filter_spy.assert_not_called()
        job_bulk.assert_not_awaited()
        note_bulk.assert_not_awaited()


# ---------------------------------------------------------------------------
# EventCreateRequest validation
# ---------------------------------------------------------------------------


class TestEventCreateRequestValidation:
    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            _create_request(capacity=0)

    def test_ends_at_must_be_after_starts_at(self) -> None:
        starts = datetime(2026, 9, 1, tzinfo=UTC)
        ends = datetime(2026, 8, 1, tzinfo=UTC)
        with pytest.raises(Exception):
            _create_request(starts_at=starts, ends_at=ends)
