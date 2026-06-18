from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.event import Event
    from hefest.models.registration import Registration


class UserRole(StrEnum):
    student = "student"
    organizer = "organizer"


class User(Model):
    """Application user — either a student or an organizer."""

    id = fields.UUIDField(primary_key=True)
    email = fields.CharField(max_length=254, unique=True)
    password_hash = fields.TextField()
    full_name = fields.CharField(max_length=255)
    role = fields.CharEnumField(UserRole, max_length=16)
    created_at = fields.DatetimeField(auto_now_add=True)

    # reverse relations (declared here for type hints only)
    events: fields.ReverseRelation[Event]
    registrations: fields.ReverseRelation[Registration]

    class Meta:
        table = "users"
