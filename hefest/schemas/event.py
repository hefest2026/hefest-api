"""Event request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from hefest.models.event import EventStatus

PositiveInt = Annotated[int, Field(ge=1)]


class EventCreateRequest(BaseModel):
    """Body for POST /events."""

    title: str
    description: str = ""
    starts_at: datetime
    ends_at: datetime | None = None
    location: str
    capacity: PositiveInt

    @model_validator(mode="after")
    def ends_after_starts(self) -> EventCreateRequest:
        """Validate ends_at is after starts_at when provided."""
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class EventUpdateRequest(BaseModel):
    """Body for PUT /events/{id} — all fields optional.

    Only include fields you want to change. ``ends_at`` accepts ``null`` to
    clear a previously set end time; other fields ignore ``null``.
    """

    title: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: str | None = None
    capacity: Annotated[int, Field(ge=1)] | None = None


class EventResponse(BaseModel):
    """Response schema for a single event (list view)."""

    id: UUID
    organizer_id: UUID
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime | None
    location: str
    capacity: int
    status: EventStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EventDetailResponse(EventResponse):
    """Response schema for GET /events/{id} — includes live seat counts."""

    confirmed_count: int
    waitlist_count: int
