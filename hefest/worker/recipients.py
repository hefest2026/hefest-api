"""Re-fetch User + Event for a claimed notification job.

Reads only the ids from the payload — never trusts payload-embedded PII.
Both rows are fetched fresh from the DB so the email reflects current state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hefest.models.event import Event
from hefest.models.user import User
from hefest.worker.errors import RecipientNotFound


@dataclass(frozen=True)
class Recipient:
    """Fresh User and (optional) Event fetched for a notification job.

    Attributes:
        user: The notification recipient.
        event: The event the job pertains to, or ``None`` for account-scoped
            jobs (e.g. ``EmailVerify``) whose payload carries no ``event_id``.
    """

    user: User
    event: Event | None


async def load(payload: dict[str, Any]) -> Recipient:
    """Load the fresh User and (if present) Event referenced by a job payload.

    Reads ONLY ``student_id`` and the optional ``event_id`` from the payload
    (never trusts payload-embedded PII) and re-fetches the row(s) from the DB.
    ``event_id`` is optional: account-scoped jobs omit it and resolve to an
    event of ``None``.

    Args:
        payload: The decoded job payload dict (from ``ClaimedJob.payload``).

    Returns:
        Recipient with the freshly fetched User and either the fetched Event or
        ``None`` when the payload carries no ``event_id``.

    Raises:
        RecipientNotFound: If ``student_id`` is absent from the payload, or if
            the user — or an ``event_id`` that IS present — no longer exists in
            the DB.  Each case is permanent: the job can never be delivered and
            must be parked as ``failed``.
    """
    try:
        student_id = payload["student_id"]
    except KeyError as exc:
        raise RecipientNotFound(f"payload missing required key: {exc}") from exc

    user = await User.get_or_none(id=student_id)
    if user is None:
        raise RecipientNotFound(f"user {student_id!r} not found")

    event_id = payload.get("event_id")
    if event_id is None:
        return Recipient(user=user, event=None)

    event = await Event.get_or_none(id=event_id)
    if event is None:
        raise RecipientNotFound(f"event {event_id!r} not found")

    return Recipient(user=user, event=event)
