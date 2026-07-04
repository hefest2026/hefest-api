"""Notifications router — the signed-in user's personal in-app feed.

Unlike the organizer-only ``/notification-jobs`` outbox view, every endpoint
here is scoped to ``current_user`` and available to any authenticated user
(student or organizer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from hefest.models.notification import Notification
from hefest.models.user import User
from hefest.routers.deps import get_current_user
from hefest.schemas.notification import NotificationResponse, UnreadCountResponse

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
) -> list[NotificationResponse]:
    """Return the caller's notifications, newest first."""
    rows = await (
        Notification.filter(user_id=user.id)
        .order_by("-created_at")
        .offset(offset)
        .limit(limit)
    )
    return [NotificationResponse.model_validate(row) for row in rows]


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    user: User = Depends(get_current_user),
) -> UnreadCountResponse:
    """Return the caller's unread notification count."""
    count = await Notification.filter(user_id=user.id, read_at=None).count()
    return UnreadCountResponse(count=count)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(user: User = Depends(get_current_user)) -> None:
    """Mark every unread notification for the caller as read (idempotent)."""
    await Notification.filter(user_id=user.id, read_at=None).update(
        read_at=datetime.now(UTC)
    )


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    notification_id: UUID,
    user: User = Depends(get_current_user),
) -> None:
    """Mark a single notification as read.

    Returns 404 if the notification does not belong to the caller. Idempotent:
    re-marking an already-read notification is a no-op, not an error.
    """
    note = await Notification.filter(id=notification_id, user_id=user.id).get_or_none()
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="notification not found"
        )
    if note.read_at is None:
        note.read_at = datetime.now(UTC)
        await note.save(update_fields=["read_at"])
