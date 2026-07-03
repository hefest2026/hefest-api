"""Pure push-notification rendering — no I/O (HEF-43 delivery side).

Mirrors ``templates.py`` but renders short push copy instead of an email, and
only for event-scoped types: ``EmailVerify`` is account-scoped (no ``event``)
and precedes device registration in the mobile app's own flow (a token is
only registered after sign-in), so it never has a push audience.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hefest.models.event import Event
from hefest.worker.errors import PermanentError


@dataclass(frozen=True)
class PushContent:
    """Rendered push notification ready to send.

    Attributes:
        title: Notification title.
        body: Notification body text.
    """

    title: str
    body: str


def render(
    event_type: str,
    event: Event | None,
    payload: dict[str, Any],
) -> PushContent:
    """Render the push notification for one event-scoped notification job.

    Pure (no I/O). Same event types as ``templates.render`` minus
    ``EmailVerify``.

    Args:
        event_type: The notification event type string from the job.
        event: The freshly-fetched Event this job pertains to.
        payload: The decoded job payload (used for type-specific extras such
            as ``waitlist_position``).

    Returns:
        PushContent with a short title and body.

    Raises:
        PermanentError: If ``event_type`` is unknown or ``event`` is ``None``.
    """
    if event is None:
        raise PermanentError(
            f"event-scoped event_type {event_type!r} has no event to render"
        )

    match event_type:
        case "RegistrationConfirmed":
            return PushContent(
                title="Registration confirmed",
                body=f"You're registered for {event.title}.",
            )

        case "RegistrationWaitlisted":
            pos = payload.get("waitlist_position")
            suffix = f" (position {pos})" if pos is not None else ""
            return PushContent(
                title="Added to waitlist",
                body=f"You're on the waitlist for {event.title}{suffix}.",
            )

        case "WaitlistPromoted":
            return PushContent(
                title="Spot opened up",
                body=f"You're now confirmed for {event.title}.",
            )

        case "RegistrationCancelled":
            return PushContent(
                title="Registration cancelled",
                body=f"Your registration for {event.title} was cancelled.",
            )

        case "EventCancelled":
            return PushContent(
                title="Event cancelled",
                body=f"{event.title} has been cancelled.",
            )

        case _:
            raise PermanentError(
                f"unknown event_type {event_type!r}: cannot render push"
            )
