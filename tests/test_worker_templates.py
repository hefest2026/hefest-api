"""Unit tests for hefest.worker.templates (HEF-39).

Templates are pure functions — no DB or SMTP interaction. User and Event
objects are constructed as ``types.SimpleNamespace`` stubs carrying only the
attributes the render function accesses (``full_name`` on User; ``title``,
``starts_at``, ``location`` on Event). This avoids Tortoise ORM initialisation
while keeping the assertions on real rendered output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hefest.models.event import Event
from hefest.models.user import User
from hefest.worker.errors import PermanentError
from hefest.worker.templates import EmailContent, render

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

_USER = SimpleNamespace(full_name="Alice Smith", email="alice@example.com")
_EVENT = SimpleNamespace(
    title="Python Workshop",
    starts_at=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
    location="Room 101",
)

# Expected formatted start time (must match _DT_FORMAT in templates.py)
_STARTS = "01 Sep 2026 at 14:00 UTC"


def _render(event_type: str, payload: dict[str, Any] | None = None) -> EmailContent:
    """Call render with the shared stubs, defaulting payload to empty."""
    return render(event_type, cast(User, _USER), cast(Event, _EVENT), payload or {})


# ---------------------------------------------------------------------------
# One test per event type
# ---------------------------------------------------------------------------


def test_registration_confirmed_subject_and_body() -> None:
    result = _render("RegistrationConfirmed")

    assert "Python Workshop" in result.subject
    assert "registered" in result.subject.lower()
    assert "Alice Smith" in result.body
    assert "Python Workshop" in result.body
    assert _STARTS in result.body
    assert "Room 101" in result.body


def test_registration_waitlisted_subject_body_and_position() -> None:
    result = _render("RegistrationWaitlisted", {"waitlist_position": 3})

    assert "waitlist" in result.subject.lower()
    assert "Python Workshop" in result.subject
    assert "Alice Smith" in result.body
    assert "Python Workshop" in result.body
    assert "Your current position: 3." in result.body
    assert _STARTS in result.body
    assert "Room 101" in result.body


def test_registration_waitlisted_without_position() -> None:
    result = _render("RegistrationWaitlisted", {})

    assert "Alice Smith" in result.body
    assert "Your current position" not in result.body


def test_waitlist_promoted_subject_and_body() -> None:
    result = _render("WaitlistPromoted")

    assert "Python Workshop" in result.subject
    assert "confirmed" in result.subject.lower()
    assert "Alice Smith" in result.body
    assert "Python Workshop" in result.body
    assert _STARTS in result.body


def test_registration_cancelled_subject_and_body() -> None:
    result = _render("RegistrationCancelled")

    assert "Python Workshop" in result.subject
    assert "cancelled" in result.subject.lower()
    assert "Alice Smith" in result.body
    assert "Python Workshop" in result.body
    assert _STARTS in result.body


def test_event_cancelled_subject_and_body() -> None:
    result = _render("EventCancelled")

    assert "Python Workshop" in result.subject
    assert "cancelled" in result.subject.lower()
    assert "Alice Smith" in result.body
    assert "Python Workshop" in result.body
    assert _STARTS in result.body


def test_unknown_event_type_raises_permanent_error() -> None:
    with pytest.raises(PermanentError):
        _render("UnknownType")
