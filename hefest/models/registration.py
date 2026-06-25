from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.indexes import PartialIndex
from tortoise.migrations.constraints import UniqueConstraint
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.event import Event
    from hefest.models.user import User


class RegistrationStatus(StrEnum):
    confirmed = "confirmed"
    waitlisted = "waitlisted"
    cancelled = "cancelled"


class Registration(Model):
    """A student's registration for an event."""

    id = fields.UUIDField(primary_key=True)
    event: fields.ForeignKeyRelation[Event] = fields.ForeignKeyField(
        "models.Event",
        related_name="registrations",
        on_delete=fields.OnDelete.CASCADE,
    )
    event_id: uuid.UUID
    student: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User",
        related_name="registrations",
        on_delete=fields.OnDelete.CASCADE,
    )
    student_id: uuid.UUID
    status = fields.CharEnumField(RegistrationStatus, max_length=16)
    registered_at = fields.DatetimeField(auto_now_add=True)
    cancelled_at = fields.DatetimeField(null=True)

    class Meta:
        table = "registrations"
        indexes = [
            ("event_id", "status"),
            ("student_id",),
            PartialIndex(
                fields=["event_id", "registered_at"],
                name="idx_registrations_waitlist_fifo",
                condition={"status": "waitlisted"},
            ),
        ]
        constraints = [
            UniqueConstraint(
                fields=("event_id", "student_id"),
                name="uq_one_active_registration_per_student",
                condition="status IN ('confirmed', 'waitlisted')",
            ),
        ]
