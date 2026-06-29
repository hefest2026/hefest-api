"""Registrations router."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from hefest.models.user import User, UserRole
from hefest.routers.deps import require_role
from hefest.schemas.registration import (
    MyRegistrationResponse,
    RegistrationResponse,
    RegistrationSummary,
)
from hefest.services import registration as reg_svc

router = APIRouter(tags=["registrations"])

_require_student = require_role(UserRole.student)
_require_organizer = require_role(UserRole.organizer)


@router.post(
    "/events/{event_id}/registrations",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_for_event(
    event_id: UUID,
    student: User = Depends(_require_student),
) -> RegistrationResponse:
    """Register the current student for a published event.

    Returns CONFIRMED when a seat is available, WAITLISTED otherwise.
    """
    return await reg_svc.register_student(student, event_id)


@router.get("/registrations/me", response_model=list[MyRegistrationResponse])
async def my_registrations(
    student: User = Depends(_require_student),
) -> list[MyRegistrationResponse]:
    """List the current student's active registrations with waitlist positions."""
    return await reg_svc.list_my_registrations(student)


@router.delete(
    "/registrations/{reg_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_registration(
    reg_id: UUID,
    student: User = Depends(_require_student),
) -> None:
    """Cancel the student's own registration.

    Only allowed before the event's ``starts_at``. Automatically promotes
    the next waitlisted student to CONFIRMED.
    """
    await reg_svc.cancel_registration(student, reg_id)


@router.get(
    "/events/{event_id}/registrations",
    response_model=list[RegistrationSummary],
)
async def event_registrations(
    event_id: UUID,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    organizer: User = Depends(_require_organizer),
) -> list[RegistrationSummary]:
    """List confirmed registrations for an organizer's own event."""
    return await reg_svc.list_event_registrations(
        organizer, event_id, limit=limit, offset=offset
    )


@router.get(
    "/events/{event_id}/waitlist",
    response_model=list[RegistrationSummary],
)
async def event_waitlist(
    event_id: UUID,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    organizer: User = Depends(_require_organizer),
) -> list[RegistrationSummary]:
    """List the FIFO waitlist for an organizer's own event."""
    return await reg_svc.list_event_registrations(
        organizer, event_id, waitlist_only=True, limit=limit, offset=offset
    )
