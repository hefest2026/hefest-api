"""Registration request/response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from hefest.models.registration import RegistrationStatus


class RegistrationResponse(BaseModel):
    """Response for POST /events/{id}/registrations."""

    id: UUID
    event_id: UUID
    student_id: UUID
    status: RegistrationStatus
    registered_at: datetime
    waitlist_position: int | None

    model_config = {"from_attributes": True}


class MyRegistrationResponse(BaseModel):
    """Single entry in GET /registrations/me."""

    id: UUID
    event_id: UUID
    status: RegistrationStatus
    registered_at: datetime
    cancelled_at: datetime | None
    waitlist_position: int | None

    model_config = {"from_attributes": True}


class RegistrationSummary(BaseModel):
    """Entry in organizer-facing confirmed / waitlist lists."""

    id: UUID
    student_id: UUID
    status: RegistrationStatus
    registered_at: datetime

    model_config = {"from_attributes": True}
