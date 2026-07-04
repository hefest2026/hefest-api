from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.event import Event
    from hefest.models.user import User


class NotificationType(StrEnum):
    """In-app notification kind.

    Values intentionally mirror the ``event_type`` strings produced by the
    email outbox (:class:`~hefest.models.notification_job.NotificationJob`), so a
    single constant travels from one call site through both the outbox job and
    the in-app row.
    """

    registration_confirmed = "RegistrationConfirmed"
    registration_waitlisted = "RegistrationWaitlisted"
    waitlist_promoted = "WaitlistPromoted"
    registration_cancelled = "RegistrationCancelled"
    event_cancelled = "EventCancelled"
    event_updated = "EventUpdated"
    welcome = "Welcome"


class Notification(Model):
    """A personal, per-user notification shown in the in-app dropdown feed.

    Created in the same transaction as the matching outbox ``NotificationJob``
    at every business-event trigger site, so an in-app notification can never
    exist without — or diverge from — its email counterpart. Unlike the outbox
    job (an email-delivery artefact), this row is recipient-scoped and carries
    per-item read/unread state via ``read_at``.
    """

    id = fields.UUIDField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User",
        related_name="notifications",
        on_delete=fields.OnDelete.CASCADE,
    )
    user_id: uuid.UUID
    # Nullable: account-scoped notifications (e.g. ``Welcome``) have no event.
    event: fields.ForeignKeyNullableRelation[Event] = fields.ForeignKeyField(
        "models.Event",
        related_name="notifications",
        on_delete=fields.OnDelete.CASCADE,
        null=True,
    )
    event_id: uuid.UUID | None
    notification_type = fields.CharEnumField(NotificationType, max_length=32)
    payload = fields.JSONField()
    read_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "notifications"
