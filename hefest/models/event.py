from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.notification_job import NotificationJob
    from hefest.models.registration import Registration
    from hefest.models.user import User


class EventStatus(StrEnum):
    draft = "draft"
    published = "published"
    cancelled = "cancelled"


class Event(Model):
    """A school event created by an organizer."""

    id = fields.UUIDField(primary_key=True)
    organizer: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User",
        related_name="events",
        on_delete=fields.OnDelete.CASCADE,
    )
    title = fields.TextField()
    description = fields.TextField(default="")
    starts_at = fields.DatetimeField()
    ends_at = fields.DatetimeField(null=True)
    location = fields.TextField()
    capacity = fields.IntField()
    status = fields.CharEnumField(EventStatus, max_length=16, default=EventStatus.draft)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    registrations: fields.ReverseRelation[Registration]
    notification_jobs: fields.ReverseRelation[NotificationJob]

    class Meta:
        table = "events"
        indexes = [
            ("organizer_id",),
        ]
