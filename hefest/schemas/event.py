"""Event request/response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from hefest.models.event import EventStatus


class EventCreateRequest(BaseModel):
    """Body for POST /events."""

    title: str
    description: str = ""
    starts_at: datetime
    ends_at: datetime | None = None
    location: str
    capacity: int

    @field_validator("capacity")
    @classmethod
    def capacity_positive(cls, v: int) -> int:
        """Validate capacity is at least 1."""
        if v < 1:
            raise ValueError("capacity must be at least 1")
        return v

    @model_validator(mode="after")
    def ends_after_starts(self) -> EventCreateRequest:
        """Validate ends_at is after starts_at when provided."""
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class EventUpdateRequest(BaseModel):
    """Body for PUT /events/{id} — all fields optional."""

    title: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: str | None = None
    capacity: int | None = None

    @field_validator("capacity")
    @classmethod
    def capacity_positive(cls, v: int | None) -> int | None:
        """Validate capacity is at least 1 when provided."""
        if v is not None and v < 1:
            raise ValueError("capacity must be at least 1")
        return v


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
