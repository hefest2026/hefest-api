"""Events router — CRUD + lifecycle actions."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from hefest.models.user import User, UserRole
from hefest.routers.deps import get_current_user, require_role
from hefest.schemas.event import (
    EventCreateRequest,
    EventDetailResponse,
    EventResponse,
    EventUpdateRequest,
)
from hefest.services import event as event_svc

router = APIRouter(prefix="/events", tags=["events"])

_require_organizer = require_role(UserRole.organizer)


@router.post("", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreateRequest,
    organizer: User = Depends(_require_organizer),
) -> EventResponse:
    """Create a new event in DRAFT status."""
    evt = await event_svc.create_event(organizer, body)
    return EventResponse.model_validate(evt)


@router.get("", response_model=list[EventResponse])
async def list_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
) -> list[EventResponse]:
    """List events visible to the caller.

    Students: published only. Organizers: own events + all published.
    """
    events = await event_svc.list_events(user, limit=limit, offset=offset)
    return [EventResponse.model_validate(e) for e in events]


@router.get("/{event_id}", response_model=EventDetailResponse)
async def get_event(
    event_id: UUID,
    user: User = Depends(get_current_user),
) -> EventDetailResponse:
    """Get event details including confirmed count and waitlist size."""
    return await event_svc.get_event_detail(user, event_id)


@router.put("/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: UUID,
    body: EventUpdateRequest,
    organizer: User = Depends(_require_organizer),
) -> EventResponse:
    """Update a DRAFT event (organizer/owner only)."""
    evt = await event_svc.update_event(organizer, event_id, body)
    return EventResponse.model_validate(evt)


@router.post("/{event_id}/publish", response_model=EventResponse)
async def publish_event(
    event_id: UUID,
    organizer: User = Depends(_require_organizer),
) -> EventResponse:
    """Publish a DRAFT event (organizer/owner only)."""
    evt = await event_svc.publish_event(organizer, event_id)
    return EventResponse.model_validate(evt)


@router.post("/{event_id}/cancel", response_model=EventResponse)
async def cancel_event(
    event_id: UUID,
    organizer: User = Depends(_require_organizer),
) -> EventResponse:
    """Cancel an event (organizer/owner only)."""
    evt = await event_svc.cancel_event(organizer, event_id)
    return EventResponse.model_validate(evt)
