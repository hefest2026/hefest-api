"""Unit tests for hefest.worker.recipients (HEF-39).

The DB is not needed — ``User.get_or_none`` and ``Event.get_or_none`` are
patched with ``AsyncMock`` so tests run fully in-process.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hefest.worker.errors import RecipientNotFound
from hefest.worker.recipients import Recipient, load

STUDENT_ID = str(uuid.uuid4())
EVENT_ID = str(uuid.uuid4())

_PAYLOAD: dict[str, str] = {
    "student_id": STUDENT_ID,
    "event_id": EVENT_ID,
}

_FAKE_USER = SimpleNamespace(id=STUDENT_ID, full_name="Bob", email="bob@example.com")
_FAKE_EVENT = SimpleNamespace(id=EVENT_ID, title="Science Fair")


async def test_load_returns_recipient_when_both_exist() -> None:
    with (
        patch(
            "hefest.worker.recipients.User.get_or_none",
            new_callable=AsyncMock,
            return_value=_FAKE_USER,
        ),
        patch(
            "hefest.worker.recipients.Event.get_or_none",
            new_callable=AsyncMock,
            return_value=_FAKE_EVENT,
        ),
    ):
        result = await load(_PAYLOAD)

    assert isinstance(result, Recipient)
    assert result.user is _FAKE_USER
    assert result.event is _FAKE_EVENT


async def test_load_raises_when_user_missing() -> None:
    with patch(
        "hefest.worker.recipients.User.get_or_none",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(RecipientNotFound, match=STUDENT_ID):
            await load(_PAYLOAD)


async def test_load_raises_when_event_missing() -> None:
    with (
        patch(
            "hefest.worker.recipients.User.get_or_none",
            new_callable=AsyncMock,
            return_value=_FAKE_USER,
        ),
        patch(
            "hefest.worker.recipients.Event.get_or_none",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        with pytest.raises(RecipientNotFound, match=EVENT_ID):
            await load(_PAYLOAD)


async def test_load_raises_when_student_id_key_absent() -> None:
    with pytest.raises(RecipientNotFound, match="student_id"):
        await load({"event_id": EVENT_ID})


async def test_load_returns_event_none_when_event_id_key_absent() -> None:
    # Account-scoped jobs (e.g. EmailVerify) omit event_id and must resolve to
    # a recipient with event=None without touching Event.get_or_none.
    with (
        patch(
            "hefest.worker.recipients.User.get_or_none",
            new_callable=AsyncMock,
            return_value=_FAKE_USER,
        ),
        patch(
            "hefest.worker.recipients.Event.get_or_none",
            new_callable=AsyncMock,
        ) as event_get,
    ):
        result = await load({"student_id": STUDENT_ID})

    assert result.user is _FAKE_USER
    assert result.event is None
    event_get.assert_not_awaited()


async def test_load_returns_event_none_when_event_id_explicitly_none() -> None:
    with patch(
        "hefest.worker.recipients.User.get_or_none",
        new_callable=AsyncMock,
        return_value=_FAKE_USER,
    ):
        result = await load({"student_id": STUDENT_ID, "event_id": None})

    assert result.event is None


async def test_load_raises_when_payload_empty() -> None:
    with pytest.raises(RecipientNotFound, match="student_id"):
        await load({})
