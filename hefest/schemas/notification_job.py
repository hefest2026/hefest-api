"""Notification job response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from hefest.models.notification_job import JobStatus


class NotificationJobResponse(BaseModel):
    """Response for GET /notification-jobs (list)."""

    id: UUID
    event_id: UUID | None
    event_type: str
    payload: dict[str, Any]
    status: JobStatus
    idempotency_key: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationJobDetailResponse(NotificationJobResponse):
    """Response for GET /notification-jobs/{id} — includes the delivery diagnostic."""

    last_error: str | None
