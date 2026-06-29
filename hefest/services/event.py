"""Event business logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import HTTPException, status
from tortoise.expressions import Q
from tortoise.functions import Count
from tortoise.transactions import in_transaction

from hefest.config import settings
from hefest.models.event import Event, EventStatus
from hefest.models.notification_job import NotificationJob
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User, UserRole
from hefest.schemas.event import (
    EventCreateRequest,
    EventDetailResponse,
    EventUpdateRequest,
)


class AnnotatedEvent(Event):
    confirmed_count: int
    waitlist_count: int


async def create_event(organizer: User, data: EventCreateRequest) -> Event:
    """Create a new event in DRAFT status for the given organizer.

    Args:
        organizer: The authenticated organizer creating the event.
        data: Validated request body.

    Returns:
        The newly created Event.
    """
    return await Event.create(
        organizer=organizer,
        title=data.title,
        description=data.description,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        location=data.location,
        capacity=data.capacity,
        status=EventStatus.draft,
    )


async def list_events(user: User, *, limit: int = 100, offset: int = 0) -> list[Event]:
    """Return events visible to the caller with confirmed registration counts.

    Students see only published events. Organizers see their own events plus
    all published events from other organizers.

    Args:
        user: The authenticated user.
        limit: Maximum rows to return (default 100).
        offset: Number of rows to skip.

    Returns:
        List of visible Event objects ordered by start date descending,
        each annotated with a ``confirmed_count`` attribute.
    """
    confirmed_annotation = Count(
        "registrations__id",
        _filter=Q(registrations__status="confirmed"),
    )
    if user.role == UserRole.student:
        return await (
            Event.filter(status=EventStatus.published)
            .annotate(confirmed_count=confirmed_annotation)
            .order_by("-starts_at")
            .offset(offset)
            .limit(limit)
        )
    # Organizer: own events (any status) OR any published event
    return await (
        Event.filter(Q(organizer=user) | Q(status=EventStatus.published))
        .annotate(confirmed_count=confirmed_annotation)
        .order_by("-starts_at")
        .offset(offset)
        .limit(limit)
    )


async def get_event_detail(user: User, event_id: UUID) -> EventDetailResponse:
    """Fetch a single event with confirmed count and waitlist size.

    All three values (event + both counts) are fetched in a single query via
    annotated COUNT aggregates. Students see published events only; organizers
    see their own drafts and any published event.

    Args:
        user: The authenticated user.
        event_id: The event UUID.

    Returns:
        EventDetailResponse with live seat counts.

    Raises:
        HTTPException 404: If the event is not found or not visible.
    """
    event = await (
        Event.filter(id=event_id)
        .annotate(
            confirmed_count=Count(
                "registrations__id",
                _filter=Q(registrations__status="confirmed"),
            ),
            waitlist_count=Count(
                "registrations__id",
                _filter=Q(registrations__status="waitlisted"),
            ),
        )
        .first()
    )

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )

    annotated_event = cast(AnnotatedEvent, event)

    _assert_visible(user, annotated_event)

    organizer = await User.get(id=annotated_event.organizer_id)

    return EventDetailResponse(
        id=annotated_event.id,
        organizer_id=annotated_event.organizer_id,
        title=annotated_event.title,
        description=annotated_event.description,
        starts_at=annotated_event.starts_at,
        ends_at=annotated_event.ends_at,
        location=annotated_event.location,
        capacity=annotated_event.capacity,
        status=annotated_event.status,
        created_at=annotated_event.created_at,
        updated_at=annotated_event.updated_at,
        confirmed_count=annotated_event.confirmed_count,
        waitlist_count=annotated_event.waitlist_count,
        organizer_name=organizer.full_name,
    )


async def update_event(user: User, event_id: UUID, data: EventUpdateRequest) -> Event:
    """Update an event owned by the caller.

    All fields may be changed regardless of event status, with one exception:
    the location cannot be changed within 2 hours of the event start.

    Partial updates are supported — only fields present in the request body
    are written. Send ``null`` for ``ends_at`` to clear it.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.
        data: Validated partial update body.

    Returns:
        The updated Event.

    Raises:
        HTTPException 404: If the event is not found or not owned by the caller.
        HTTPException 409: If the location is being changed within 2 hours of start.
    """
    event = await Event.filter(id=event_id).select_for_update().get_or_none()
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )

    _assert_owner(user, event)

    update_data = data.model_dump(exclude_unset=True)

    if "location" in update_data:
        cutoff = datetime.now(UTC) + timedelta(hours=settings.event_location_lock_hours)
        starts_at = event.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=UTC)

        if starts_at <= cutoff:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"location cannot be changed within "
                    f"{settings.event_location_lock_hours} hours of the event start"
                ),
            )

    if update_data:
        event.update_from_dict(update_data)
        await event.save(update_fields=list(update_data.keys()))

    return event


async def publish_event(user: User, event_id: UUID) -> Event:
    """Transition a DRAFT event to PUBLISHED.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.

    Returns:
        The updated Event with status PUBLISHED.

    Raises:
        HTTPException 404: If the event is not found or not owned by the caller.
        HTTPException 409: If the event is not in DRAFT status.
    """
    event = await _get_owned_draft(user, event_id)
    event.status = EventStatus.published
    await event.save(update_fields=["status", "updated_at"])
    return event


async def cancel_event(user: User, event_id: UUID) -> Event:
    """Cancel an event owned by the caller, fanning out EventCancelled jobs.

    Both draft and published events can be cancelled. Already-cancelled events
    are returned as-is without enqueueing any new jobs (idempotent). Past
    events (starts_at <= now) cannot be cancelled.

    The cancel mutation and the notification fan-out run in a single
    transaction: the event row is locked first (select_for_update), then one
    NotificationJob is bulk-created per confirmed or waitlisted registration.
    The statement-level AFTER INSERT trigger fires exactly one pg_notify for
    the whole bulk insert, so each row is handled as a normal one-email job.

    Note: N rows in one bulk_create is trivial at school-event capacity.
    Chunk bulk_create if capacity ever grows.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.

    Returns:
        The updated Event with status CANCELLED.

    Raises:
        HTTPException 404: If the event is not found or not owned by the caller.
        HTTPException 409: If the event has already started.
    """
    async with in_transaction() as conn:
        event = (
            await Event.filter(id=event_id)
            .using_db(conn)
            .select_for_update()
            .get_or_none()
        )
        if event is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )
        _assert_owner(user, event)

        if event.status == EventStatus.cancelled:
            return event

        starts_at = event.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=UTC)
        if starts_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot cancel an event that has already started",
            )

        event.status = EventStatus.cancelled
        await event.save(update_fields=["status", "updated_at"], using_db=conn)

        regs = await Registration.filter(
            event_id=event_id,
            status__in=[RegistrationStatus.confirmed, RegistrationStatus.waitlisted],
        ).using_db(conn)

        occurred_at = datetime.now(UTC).isoformat()
        jobs = [
            NotificationJob(
                event_id=event_id,
                event_type="EventCancelled",
                payload={
                    "event_id": str(event_id),
                    "student_id": str(reg.student_id),
                    "registration_id": str(reg.id),
                    "occurred_at": occurred_at,
                },
                idempotency_key=f"{reg.id}:EventCancelled",
            )
            for reg in regs
        ]
        if jobs:
            await NotificationJob.bulk_create(jobs, using_db=conn)

    return event


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_visible(user: User, event: Event) -> None:
    """Raise 404 if the event is not visible to the caller.

    Students see published only. Organizers see own events + any published.
    """
    if user.role == UserRole.student:
        if event.status != EventStatus.published:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )
    else:
        if event.status != EventStatus.published and event.organizer_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )


def _assert_owner(user: User, event: Event) -> None:
    """Raise 404 if the caller does not own the event.

    Returns 404 (not 403) to avoid leaking the existence of resources the
    caller has no access to.
    """
    if event.organizer_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )


async def _get_owned_draft(user: User, event_id: UUID) -> Event:
    """Fetch the event, assert ownership and DRAFT status.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.

    Returns:
        The Event in DRAFT status owned by the caller.

    Raises:
        HTTPException 404: If not found or not owned by the caller.
        HTTPException 409: If not in DRAFT.
    """
    event = await Event.get_or_none(id=event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )
    _assert_owner(user, event)
    if event.status != EventStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"event is not in draft status (current: {event.status})",
        )
    return event
