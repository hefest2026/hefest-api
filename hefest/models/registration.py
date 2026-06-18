from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.event import Event
    from hefest.models.user import User


class RegistrationStatus(StrEnum):
    confirmed = "confirmed"
    waitlisted = "waitlisted"
    cancelled = "cancelled"


class Registration(Model):
    """A student's registration for an event.

    The partial unique index ``uq_one_active_registration_per_student`` is
    defined in the migration — Tortoise ORM does not yet emit partial indexes
    from the model Meta, so it is added manually in the initial migration.
    """

    id = fields.UUIDField(primary_key=True)
    event: fields.ForeignKeyRelation[Event] = fields.ForeignKeyField(
        "models.Event",
        related_name="registrations",
        on_delete=fields.OnDelete.CASCADE,
    )
    student: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User",
        related_name="registrations",
        on_delete=fields.OnDelete.CASCADE,
    )
    status = fields.CharEnumField(RegistrationStatus, max_length=16)
    registered_at = fields.DatetimeField(auto_now_add=True)
    cancelled_at = fields.DatetimeField(null=True)

    class Meta:
        table = "registrations"
        indexes = [
            ("event_id", "status"),
            ("student_id",),
        ]
