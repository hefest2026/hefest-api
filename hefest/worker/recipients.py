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
    """Fresh User and Event fetched for a notification job.

    Attributes:
        user: The notification recipient.
        event: The event the job pertains to.
    """

    user: User
    event: Event


async def load(payload: dict[str, Any]) -> Recipient:
    """Load the fresh User and Event referenced by a job payload.

    Reads ONLY ``student_id`` and ``event_id`` from the payload (never trusts
    payload-embedded PII) and re-fetches both rows from the DB.

    Args:
        payload: The decoded job payload dict (from ``ClaimedJob.payload``).

    Returns:
        Recipient with freshly fetched User and Event.

    Raises:
        RecipientNotFound: If ``student_id`` or ``event_id`` is absent from
            the payload, or if either row no longer exists in the DB.  Both
            cases are permanent — the job can never be delivered and must be
            parked as ``failed``.
    """
    try:
        student_id = payload["student_id"]
        event_id = payload["event_id"]
    except KeyError as exc:
        raise RecipientNotFound(f"payload missing required key: {exc}") from exc

    user = await User.get_or_none(id=student_id)
    if user is None:
        raise RecipientNotFound(f"user {student_id!r} not found")

    event = await Event.get_or_none(id=event_id)
    if event is None:
        raise RecipientNotFound(f"event {event_id!r} not found")

    return Recipient(user=user, event=event)
