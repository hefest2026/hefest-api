"""Schemas for organizer dashboard statistics."""

from __future__ import annotations

from pydantic import BaseModel


class OrganizerStatsResponse(BaseModel):
    """Aggregate metrics for an organizer's own events.

    All registration counts are naturally published-only (registration requires a
    published event), so ``total_capacity`` is scoped to published events to keep
    the ``total_confirmed / total_capacity`` occupancy ratio coherent.

    Attributes:
        events_total: Count of the organizer's events in any status.
        events_draft: Count of the organizer's draft events.
        events_published: Count of the organizer's published events.
        events_upcoming: Published events whose ``starts_at`` is in the future.
        total_capacity: Sum of capacity over the organizer's published events.
        total_confirmed: Confirmed registrations over the organizer's events.
        total_waitlisted: Waitlisted registrations over the organizer's events.
        new_registrations_7d: Confirmed registrations in the last seven days.
    """

    events_total: int
    events_draft: int
    events_published: int
    events_upcoming: int
    total_capacity: int
    total_confirmed: int
    total_waitlisted: int
    new_registrations_7d: int
