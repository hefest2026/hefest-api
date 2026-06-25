"""Unit tests for hefest.services.event.

All Tortoise ORM calls are mocked so no database is required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from hefest.models.event import EventStatus
from hefest.models.user import UserRole
from hefest.schemas.event import EventCreateRequest, EventUpdateRequest
from hefest.services import event as svc

UTC = UTC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


class TestCancelEvent:
    async def test_draft_event_gets_cancelled(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.save = AsyncMock()

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            result = await svc.cancel_event(organizer, evt.id)

        assert result.status == EventStatus.cancelled

    async def test_already_cancelled_is_idempotent(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.cancelled)
        evt.save = AsyncMock()

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            result = await svc.cancel_event(organizer, evt.id)

        assert result.status == EventStatus.cancelled
        evt.save.assert_not_awaited()

    async def test_wrong_owner_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=uuid.uuid4(), status=EventStatus.published)

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            with pytest.raises(HTTPException) as exc:
                await svc.cancel_event(organizer, evt.id)

        assert exc.value.status_code == 404

    async def test_past_event_raises_409(self) -> None:
        organizer = _user(UserRole.organizer)
        past = datetime(2020, 1, 1, tzinfo=UTC)
        evt = _event(
            organizer_id=organizer.id, status=EventStatus.published, starts_at=past
        )
        evt.save = AsyncMock()

        with patch.object(svc.Event, "get_or_none", new=AsyncMock(return_value=evt)):
            with pytest.raises(HTTPException) as exc:
                await svc.cancel_event(organizer, evt.id)

        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# update_event
# ---------------------------------------------------------------------------


class TestUpdateEvent:
    def _mock_filter_chain(self, evt: Any) -> MagicMock:
        """Build a mock for Event.filter(...).select_for_update().get_or_none()."""
        qs = MagicMock()
        qs.select_for_update.return_value = qs
        qs.get_or_none = AsyncMock(return_value=evt)
        return qs

    async def test_partial_update_applies_fields(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=organizer.id, status=EventStatus.draft)
        evt.save = AsyncMock()
        evt.update_from_dict = MagicMock()

        data = EventUpdateRequest(title="New Title", capacity=200)

        with patch.object(
            svc.Event, "filter", return_value=self._mock_filter_chain(evt)
        ):
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

        with patch.object(
            svc.Event, "filter", return_value=self._mock_filter_chain(evt)
        ):
            await svc.update_event(organizer, evt.id, data)

        evt.update_from_dict.assert_called_once()

    async def test_wrong_owner_raises_404(self) -> None:
        organizer = _user(UserRole.organizer)
        evt = _event(organizer_id=uuid.uuid4(), status=EventStatus.draft)

        with patch.object(
            svc.Event, "filter", return_value=self._mock_filter_chain(evt)
        ):
            with pytest.raises(HTTPException) as exc:
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

        with patch.object(
            svc.Event, "filter", return_value=self._mock_filter_chain(evt)
        ):
            with pytest.raises(HTTPException) as exc:
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

        with patch.object(
            svc.Event, "filter", return_value=self._mock_filter_chain(evt)
        ):
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

        with patch.object(
            svc.Event, "filter", return_value=self._mock_filter_chain(evt)
        ):
            await svc.update_event(organizer, evt.id, data)

        called_dict = evt.update_from_dict.call_args[0][0]
        assert list(called_dict.keys()) == ["title"]


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
