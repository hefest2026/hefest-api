"""Pure email rendering — no I/O.

Given an ``event_type`` and the resolved ``User`` / ``Event`` objects, returns
an ``EmailContent`` ready to hand to the mailer.  The ``render`` function is
pure: same inputs always produce the same output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hefest.models.event import Event
from hefest.models.user import User
from hefest.worker.errors import PermanentError

# Single datetime format used throughout all templates (DRY).
_DT_FORMAT = "%d %b %Y at %H:%M UTC"


def _fmt_dt(dt: datetime) -> str:
    """Format a datetime for inclusion in email copy.

    Args:
        dt: The datetime to format (assumed UTC).

    Returns:
        Human-readable string, e.g. ``"28 Jun 2026 at 14:00 UTC"``.
    """
    return dt.strftime(_DT_FORMAT)


@dataclass(frozen=True)
class EmailContent:
    """Rendered email ready to send.

    Attributes:
        subject: Plain-text subject line.
        body: Plain-text body.
    """

    subject: str
    body: str


def render(
    event_type: str,
    user: User,
    event: Event | None,
    payload: dict[str, Any],
    verify_link: str | None = None,
) -> EmailContent:
    """Render the email for one notification job.  Pure (no I/O).

    Args:
        event_type: The notification event type string from the job.
        user: The freshly-fetched User (recipient).
        event: The freshly-fetched Event, or ``None`` for account-scoped jobs
            (``EmailVerify``) that have no event.
        payload: The decoded job payload (used for type-specific extras such
            as ``waitlist_position``).
        verify_link: The pre-built verification URL, required for and only used
            by the ``EmailVerify`` type.

    Returns:
        EmailContent with subject and plain-text body.

    Raises:
        PermanentError: If ``event_type`` is unknown, if an event-scoped type is
            given no ``event``, or if ``EmailVerify`` is given no ``verify_link``.
            The job must be parked as ``failed``, not retried.
    """
    if event_type == "EmailVerify":
        if verify_link is None:
            raise PermanentError("EmailVerify job has no verify_link")
        return EmailContent(
            subject="Verify your Hefest email address",
            body=(
                f"Hi {user.full_name},\n\n"
                "Welcome to Hefest. Please confirm your email address by "
                "opening the link below:\n\n"
                f"{verify_link}\n\n"
                "If you did not create this account, you can ignore this email."
            ),
        )

    # Every remaining type is event-scoped and dereferences the event.
    if event is None:
        raise PermanentError(
            f"event-scoped event_type {event_type!r} has no event to render"
        )
    starts = _fmt_dt(event.starts_at)

    match event_type:
        case "RegistrationConfirmed":
            return EmailContent(
                subject=f"You're registered for {event.title}",
                body=(
                    f"Hi {user.full_name},\n\n"
                    f"Your registration for {event.title} is confirmed.\n\n"
                    f"When: {starts}\n"
                    f"Where: {event.location}\n\n"
                    "See you there."
                ),
            )

        case "RegistrationWaitlisted":
            pos = payload.get("waitlist_position")
            pos_line = f"Your current position: {pos}.\n" if pos is not None else ""
            return EmailContent(
                subject=f"You're on the waitlist for {event.title}",
                body=(
                    f"Hi {user.full_name},\n\n"
                    f"The event {event.title} is currently full, "
                    f"but you have been added to the waitlist.\n\n"
                    f"{pos_line}"
                    f"When: {starts}\n"
                    f"Where: {event.location}\n\n"
                    "We will notify you if a spot opens up."
                ),
            )

        case "WaitlistPromoted":
            return EmailContent(
                subject=f"A spot opened up: you're confirmed for {event.title}",
                body=(
                    f"Hi {user.full_name},\n\n"
                    f"Good news! A spot opened up and your registration for "
                    f"{event.title} is now confirmed.\n\n"
                    f"When: {starts}\n"
                    f"Where: {event.location}\n\n"
                    "See you there."
                ),
            )

        case "RegistrationCancelled":
            return EmailContent(
                subject=f"Your registration for {event.title} was cancelled",
                body=(
                    f"Hi {user.full_name},\n\n"
                    f"Your registration for {event.title} has been cancelled.\n\n"
                    f"When: {starts}\n"
                    f"Where: {event.location}\n\n"
                    "If this was unexpected, please contact the organizer."
                ),
            )

        case "EventCancelled":
            return EmailContent(
                subject=f"{event.title} has been cancelled",
                body=(
                    f"Hi {user.full_name},\n\n"
                    f"We regret to inform you that {event.title} "
                    f"has been cancelled.\n\n"
                    f"Scheduled for: {starts}\n"
                    f"Location: {event.location}\n\n"
                    "We apologise for any inconvenience."
                ),
            )

        case _:
            raise PermanentError(
                f"unknown event_type {event_type!r}: cannot render email"
            )
