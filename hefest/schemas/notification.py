"""In-app notification response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from hefest.models.notification import NotificationType


class NotificationResponse(BaseModel):
    """Response for GET /notifications (list)."""

    id: UUID
    event_id: UUID | None
    notification_type: NotificationType
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UnreadCountResponse(BaseModel):
    """Response for GET /notifications/unread-count."""

    count: int
