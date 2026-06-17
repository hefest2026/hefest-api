from __future__ import annotations

from enum import StrEnum

from tortoise import fields
from tortoise.models import Model


class DeliveryStatus(StrEnum):
    processing = "processing"
    completed = "completed"
    failed = "failed"


class NotificationLog(Model):
    """Delivery log written by the C++ worker.

    Claimed via ``INSERT ... ON CONFLICT DO NOTHING`` on ``idempotency_key``
    before touching SMTP. This provides at-most-once-per-idempotency-key
    delivery semantics across worker replicas.
    """

    id = fields.UUIDField(primary_key=True)
    idempotency_key = fields.CharField(max_length=512, unique=True)
    status = fields.CharEnumField(DeliveryStatus, max_length=16)
    attempts = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "notification_log"
