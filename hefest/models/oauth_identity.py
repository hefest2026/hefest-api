from __future__ import annotations

from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.models import Model

if TYPE_CHECKING:
    from hefest.models.user import User


class OAuthIdentity(Model):
    """Links a provider identity (provider + subject) to a Hefest user."""

    id = fields.UUIDField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="oauth_identities", on_delete=fields.CASCADE
    )
    provider = fields.CharField(max_length=32)  # 'google' | 'microsoft'
    subject = fields.CharField(max_length=255)  # provider's stable OpenID sub
    email = fields.CharField(max_length=254)  # refreshed on each login
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "oauth_identities"
        unique_together = (("provider", "subject"),)
