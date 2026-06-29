"""Notification jobs router — read-only outbox view for organizers."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from hefest.models.notification_job import NotificationJob
from hefest.models.user import User, UserRole
from hefest.routers.deps import require_role
from hefest.schemas.notification_job import (
    NotificationJobDetailResponse,
    NotificationJobResponse,
)

router = APIRouter(prefix="/notification-jobs", tags=["notification_jobs"])

_require_organizer = require_role(UserRole.organizer)


@router.get("", response_model=list[NotificationJobResponse])
async def list_notification_jobs(
    event_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    organizer: User = Depends(_require_organizer),
) -> list[NotificationJobResponse]:
    """List outbox jobs for events owned by the current organizer.

    Pass ``?event_id=<uuid>`` to filter to a single event.
    """
    qs = NotificationJob.filter(event__organizer=organizer)
    if event_id is not None:
        qs = qs.filter(event_id=event_id)

    jobs = await qs.order_by("-created_at").offset(offset).limit(limit)
    return [NotificationJobResponse.model_validate(j) for j in jobs]


@router.get("/{job_id}", response_model=NotificationJobDetailResponse)
async def get_notification_job(
    job_id: UUID,
    organizer: User = Depends(_require_organizer),
) -> NotificationJobDetailResponse:
    """Get a single outbox job with its delivery status and diagnostic."""
    job = await NotificationJob.filter(
        id=job_id, event__organizer=organizer
    ).get_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="notification job not found",
        )

    return NotificationJobDetailResponse(
        id=job.id,
        event_id=job.event_id,
        event_type=job.event_type,
        payload=job.payload,
        status=job.status,
        idempotency_key=job.idempotency_key,
        created_at=job.created_at,
        updated_at=job.updated_at,
        last_error=job.last_error,
    )
