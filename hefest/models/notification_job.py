from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.event import Event


class JobStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class NotificationJob(Model):
    """Postgres-outbox row — single source of truth for enqueue AND delivery.

    Written by the API in the same transaction as the triggering registration
    change, then consumed directly by the delivery worker. There is no Redis and
    no separate delivery log: the worker claims ``pending`` rows, leases them via
    ``locked_by``/``heartbeat_at`` while ``processing``, and finalizes each row
    to ``completed`` or ``failed`` in place, tracking ``attempts``,
    ``next_attempt_at`` (retry backoff) and ``last_error`` on the row itself.
    """

    id = fields.UUIDField(primary_key=True)
    # Nullable: event-scoped jobs (registration changes) reference their Event;
    # account-scoped jobs (e.g. ``EmailVerify``) have no event and store NULL.
    event: fields.ForeignKeyNullableRelation[Event] = fields.ForeignKeyField(
        "models.Event",
        related_name="notification_jobs",
        on_delete=fields.OnDelete.CASCADE,
        null=True,
    )
    event_id: uuid.UUID | None
    event_type = fields.TextField()
    payload = fields.JSONField()
    status = fields.CharEnumField(JobStatus, max_length=16, default=JobStatus.pending)
    idempotency_key = fields.CharField(max_length=512, unique=True)
    attempts = fields.IntField(default=0)
    locked_by = fields.TextField(null=True)
    heartbeat_at = fields.DatetimeField(null=True)
    next_attempt_at = fields.DatetimeField(auto_now_add=True)
    last_error = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "notification_jobs"
