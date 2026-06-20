from __future__ import annotations

from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.user import User


class RefreshToken(Model):
    """Server-side record of an issued refresh token (stores hash only)."""

    id = fields.UUIDField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="refresh_tokens", on_delete=fields.CASCADE
    )
    # SHA-256 hex of the opaque token (64 chars); UNIQUE -> B-tree index
    token_hash = fields.CharField(max_length=64, unique=True)
    expires_at = fields.DatetimeField()
    revoked_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "refresh_tokens"
