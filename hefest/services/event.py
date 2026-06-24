"""Event business logic."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from tortoise.expressions import Q

from hefest.models.event import Event, EventStatus
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User, UserRole
from hefest.schemas.event import (
    EventCreateRequest,
    EventDetailResponse,
    EventUpdateRequest,
)


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


async def list_events(user: User) -> list[Event]:
    """Return events visible to the caller.

    Students see only published events. Organizers see their own drafts and
    all published events.

    Args:
        user: The authenticated user.

    Returns:
        List of visible Event objects.
    """
    if user.role == UserRole.student:
        return await Event.filter(status=EventStatus.published).all()
    # Organizer: own events (any status) OR any published event
    return await Event.filter(Q(organizer=user) | Q(status=EventStatus.published)).all()


async def get_event_detail(user: User, event_id: UUID) -> EventDetailResponse:
    """Fetch a single event with confirmed count and waitlist size.

    Students can only see published events. Organizers can see their own
    drafts and any published event.

    Args:
        user: The authenticated user.
        event_id: The event UUID.

    Returns:
        EventDetailResponse with live seat counts.

    Raises:
        HTTPException 404: If the event is not found or not visible to the caller.
    """
    event = await Event.get_or_none(id=event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )

    _assert_visible(user, event)

    confirmed_count = await Registration.filter(
        event=event, status=RegistrationStatus.confirmed
    ).count()
    waitlist_count = await Registration.filter(
        event=event, status=RegistrationStatus.waitlisted
    ).count()

    return EventDetailResponse(
        id=event.id,
        organizer_id=event.organizer_id,
        title=event.title,
        description=event.description,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        location=event.location,
        capacity=event.capacity,
        status=event.status,
        created_at=event.created_at,
        updated_at=event.updated_at,
        confirmed_count=confirmed_count,
        waitlist_count=waitlist_count,
    )


async def update_event(user: User, event_id: UUID, data: EventUpdateRequest) -> Event:
    """Update a DRAFT event owned by the caller.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.
        data: Validated partial update body.

    Returns:
        The updated Event.

    Raises:
        HTTPException 404: If the event is not found.
        HTTPException 403: If the caller does not own the event.
        HTTPException 409: If the event is not in DRAFT status.
    """
    event = await _get_owned_draft(user, event_id)

    update_fields: list[str] = []
    for field in (
        "title",
        "description",
        "starts_at",
        "ends_at",
        "location",
        "capacity",
    ):
        value = getattr(data, field)
        if value is not None:
            setattr(event, field, value)
            update_fields.append(field)

    if update_fields:
        update_fields.append("updated_at")
        await event.save(update_fields=update_fields)

    return event


async def publish_event(user: User, event_id: UUID) -> Event:
    """Transition a DRAFT event to PUBLISHED.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.

    Returns:
        The updated Event with status PUBLISHED.

    Raises:
        HTTPException 404: If the event is not found.
        HTTPException 403: If the caller does not own the event.
        HTTPException 409: If the event is not in DRAFT status.
    """
    event = await _get_owned_draft(user, event_id)
    event.status = EventStatus.published
    await event.save(update_fields=["status", "updated_at"])
    return event


async def cancel_event(user: User, event_id: UUID) -> Event:
    """Cancel an event owned by the caller.

    A published event can be cancelled; a draft can also be cancelled.
    Already cancelled events are idempotent (return as-is).

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.

    Returns:
        The updated Event with status CANCELLED.

    Raises:
        HTTPException 404: If the event is not found.
        HTTPException 403: If the caller does not own the event.
    """
    event = await Event.get_or_none(id=event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )
    _assert_owner(user, event)

    if event.status == EventStatus.cancelled:
        return event

    event.status = EventStatus.cancelled
    await event.save(update_fields=["status", "updated_at"])
    return event


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_visible(user: User, event: Event) -> None:
    """Raise 404 if the event is not visible to the caller.

    Students see published only. Organizers see own drafts + any published.
    """
    if user.role == UserRole.student:
        if event.status != EventStatus.published:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )
    else:
        # Organizer: own event OR published
        if event.status != EventStatus.published and str(event.organizer_id) != str(
            user.id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )


def _assert_owner(user: User, event: Event) -> None:
    """Raise 403 if the caller does not own the event."""
    if str(event.organizer_id) != str(user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="you do not own this event",
        )


async def _get_owned_draft(user: User, event_id: UUID) -> Event:
    """Fetch the event, assert ownership and DRAFT status.

    Args:
        user: The authenticated organizer.
        event_id: The event UUID.

    Returns:
        The Event in DRAFT status owned by the caller.

    Raises:
        HTTPException 404: If not found.
        HTTPException 403: If not the owner.
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
