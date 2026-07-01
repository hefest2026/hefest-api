"""Organizer dashboard statistics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hefest.models.user import User, UserRole
from hefest.routers.deps import require_role
from hefest.schemas.stats import OrganizerStatsResponse
from hefest.services import stats as stats_svc

router = APIRouter(tags=["stats"])

_require_organizer = require_role(UserRole.organizer)


@router.get("/stats", response_model=OrganizerStatsResponse)
async def get_stats(
    organizer: User = Depends(_require_organizer),
) -> OrganizerStatsResponse:
    """Return dashboard aggregates for the authenticated organizer's events."""
    return await stats_svc.compute_organizer_stats(organizer)
