"""Aggregate organizer dashboard statistics from events and registrations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from tortoise.functions import Sum

from hefest.models.event import Event, EventStatus
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User
from hefest.schemas.stats import OrganizerStatsResponse

NEW_REGISTRATION_WINDOW = timedelta(days=7)


async def _sum_published_capacity(organizer_id: str) -> int:
    """Return the summed capacity across an organizer's published events."""
    rows = (
        await Event.filter(organizer_id=organizer_id, status=EventStatus.published)
        .annotate(total=Sum("capacity"))
        .values("total")
    )
    return rows[0]["total"] or 0 if rows else 0


async def compute_organizer_stats(organizer: User) -> OrganizerStatsResponse:
    """Compute dashboard aggregates scoped to an organizer's own events.

    All counts run as independent aggregate queries concurrently — no per-event
    fan-out. Registrations are joined back to their event's ``organizer_id`` so a
    student cannot influence another organizer's numbers.

    Args:
        organizer: The authenticated organizer.

    Returns:
        The populated :class:`OrganizerStatsResponse`.
    """
    organizer_id = str(organizer.id)
    now = datetime.now(UTC)
    own = Event.filter(organizer_id=organizer_id)
    own_regs = Registration.filter(event__organizer_id=organizer_id)

    (
        events_total,
        events_draft,
        events_published,
        events_upcoming,
        total_capacity,
        total_confirmed,
        total_waitlisted,
        new_registrations_7d,
    ) = await asyncio.gather(
        own.count(),
        own.filter(status=EventStatus.draft).count(),
        own.filter(status=EventStatus.published).count(),
        own.filter(status=EventStatus.published, starts_at__gt=now).count(),
        _sum_published_capacity(organizer_id),
        own_regs.filter(status=RegistrationStatus.confirmed).count(),
        own_regs.filter(status=RegistrationStatus.waitlisted).count(),
        own_regs.filter(
            status=RegistrationStatus.confirmed,
            registered_at__gte=now - NEW_REGISTRATION_WINDOW,
        ).count(),
    )

    return OrganizerStatsResponse(
        events_total=events_total,
        events_draft=events_draft,
        events_published=events_published,
        events_upcoming=events_upcoming,
        total_capacity=total_capacity,
        total_confirmed=total_confirmed,
        total_waitlisted=total_waitlisted,
        new_registrations_7d=new_registrations_7d,
    )
