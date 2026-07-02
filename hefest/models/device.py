from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.user import User


class DevicePlatform(StrEnum):
    ios = "ios"
    android = "android"


class Device(Model):
    """A device that receives push notifications for a user (Expo push token)."""

    id = fields.UUIDField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User",
        related_name="devices",
        on_delete=fields.OnDelete.CASCADE,
    )
    user_id: uuid.UUID
    # Expo push tokens look like ``ExponentPushToken[...]``; globally unique so a
    # reinstalled or re-bound device maps to exactly one row.
    expo_push_token = fields.CharField(max_length=255, unique=True)
    platform = fields.CharEnumField(DevicePlatform, max_length=8)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "devices"
