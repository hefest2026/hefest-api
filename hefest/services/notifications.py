"""In-app notification creation helper — always transactional.

Called immediately alongside each existing ``NotificationJob`` construction,
inside the *same* transaction, so an in-app notification can never exist
without (or diverge from) its matching outbox job.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from tortoise import BaseDBAsyncClient

from hefest.models.notification import Notification, NotificationType


async def notify(
    *,
    user_id: UUID,
    event_id: UUID | None,
    notification_type: NotificationType,
    payload: dict[str, Any],
    using_db: BaseDBAsyncClient,
) -> None:
    """Create an in-app :class:`Notification` row in the caller's transaction.

    Args:
        user_id: The recipient user's id.
        event_id: The related event's id, or ``None`` for account-scoped types.
        notification_type: The notification kind (mirrors the outbox event type).
        payload: Type-specific extras, mirroring the outbox job payload.
        using_db: The active transaction connection to write within.
    """
    await Notification.create(
        user_id=user_id,
        event_id=event_id,
        notification_type=notification_type,
        payload=payload,
        using_db=using_db,
    )
