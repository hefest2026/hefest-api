from __future__ import annotations

from enum import StrEnum

from tortoise import fields
from tortoise.models import Model


class JobStatus(StrEnum):
    pending = "pending"
    published = "published"


class NotificationJob(Model):
    """Transactional outbox row — bridges DB writes to Redis Streams.

    Written by the API in the same transaction as the triggering registration
    change. The relay polls ``pending`` rows and publishes them to Redis.
    """

    id = fields.UUIDField(primary_key=True)
    event: fields.ForeignKeyRelation[Event] = fields.ForeignKeyField(  # type: ignore[name-defined]  # noqa: F821
        "models.Event",
        related_name="notification_jobs",
        on_delete=fields.OnDelete.CASCADE,
    )
    event_type = fields.TextField()
    payload = fields.JSONField()
    status = fields.CharEnumField(JobStatus, max_length=16, default=JobStatus.pending)
    idempotency_key = fields.CharField(max_length=512, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "notification_jobs"
