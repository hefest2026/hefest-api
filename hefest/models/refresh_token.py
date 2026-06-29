from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.user import User


class RefreshClient(StrEnum):
    """The client a refresh token was issued to.

    The value is fixed at issuance and dictates how a rotated token is
    delivered: ``web`` tokens are returned only via the httpOnly
    ``hefest_refresh`` cookie, ``mobile`` tokens only in the response body
    (for the device secure keystore). Binding delivery to the token — rather
    than to a per-request flag — prevents a browser-XSS attacker holding the
    cookie from converting it into a body token.
    """

    web = "web"
    mobile = "mobile"


class RefreshToken(Model):
    """Server-side record of an issued refresh token (stores hash only)."""

    id = fields.UUIDField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="refresh_tokens", on_delete=fields.CASCADE
    )
    # SHA-256 hex of the opaque token (64 chars); UNIQUE -> B-tree index
    token_hash = fields.CharField(max_length=64, unique=True)
    client = fields.CharEnumField(
        RefreshClient, max_length=8, default=RefreshClient.web
    )
    expires_at = fields.DatetimeField()
    revoked_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "refresh_tokens"
