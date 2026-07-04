"""Tests for the notifications router — the user's personal in-app feed.

Endpoint behaviour (list ordering, unread count, idempotent mark-read, bulk
mark-all, and cross-user isolation) runs against the ephemeral testcontainers
Postgres via the ``db`` fixture. Each endpoint function is called directly with
the resolved ``User`` (the ``Depends(get_current_user)`` is bypassed), matching
the pattern in ``test_users_profile.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from hefest.models.notification import Notification, NotificationType
from hefest.models.user import User, UserRole
from hefest.routers.notifications import (
    list_notifications,
    mark_all_read,
    mark_read,
    unread_count,
)


async def _make_user(role: UserRole = UserRole.student) -> User:
    """Create a verified user with a unique email."""
    return await User.create(
        email=f"notif-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        full_name="Notify Tester",
        role=role,
        email_verified_at=datetime.now(UTC),
    )


async def _make_notification(
    user: User,
    *,
    notification_type: NotificationType = NotificationType.welcome,
    read: bool = False,
) -> Notification:
    """Create a notification row for ``user``, optionally already read."""
    return await Notification.create(
        user_id=user.id,
        event_id=None,
        notification_type=notification_type,
        payload={"student_id": str(user.id)},
        read_at=datetime.now(UTC) if read else None,
    )


@pytest.mark.integration
async def test_list_returns_only_own_newest_first(db: None) -> None:
    user = await _make_user()
    other = await _make_user()
    try:
        older = await _make_notification(user)
        newer = await _make_notification(
            user, notification_type=NotificationType.registration_confirmed
        )
        foreign = await _make_notification(other)  # must never appear in user's feed

        rows = await list_notifications(limit=50, offset=0, user=user)

        ids = [r.id for r in rows]
        assert ids == [newer.id, older.id]  # newest first, own rows only
        assert foreign.id not in ids
    finally:
        await Notification.filter(user_id__in=[user.id, other.id]).delete()
        await user.delete()
        await other.delete()


@pytest.mark.integration
async def test_unread_count_ignores_read_and_other_users(db: None) -> None:
    user = await _make_user()
    other = await _make_user()
    try:
        await _make_notification(user, read=False)
        await _make_notification(user, read=False)
        await _make_notification(user, read=True)  # read → excluded
        await _make_notification(other, read=False)  # other user → excluded

        result = await unread_count(user=user)

        assert result.count == 2
    finally:
        await Notification.filter(user_id__in=[user.id, other.id]).delete()
        await user.delete()
        await other.delete()


@pytest.mark.integration
async def test_mark_read_is_idempotent(db: None) -> None:
    user = await _make_user()
    try:
        note = await _make_notification(user, read=False)

        await mark_read(notification_id=note.id, user=user)
        refreshed = await Notification.get(id=note.id)
        assert refreshed.read_at is not None
        first_read_at = refreshed.read_at

        # Re-marking an already-read notification is a no-op, not an error.
        await mark_read(notification_id=note.id, user=user)
        again = await Notification.get(id=note.id)
        assert again.read_at == first_read_at
    finally:
        await Notification.filter(user_id=user.id).delete()
        await user.delete()


@pytest.mark.integration
async def test_mark_all_read_only_affects_caller(db: None) -> None:
    user = await _make_user()
    other = await _make_user()
    try:
        await _make_notification(user, read=False)
        await _make_notification(user, read=False)
        other_note = await _make_notification(other, read=False)

        await mark_all_read(user=user)

        assert await Notification.filter(user_id=user.id, read_at=None).count() == 0
        # The other user's notification is untouched.
        untouched = await Notification.get(id=other_note.id)
        assert untouched.read_at is None
    finally:
        await Notification.filter(user_id__in=[user.id, other.id]).delete()
        await user.delete()
        await other.delete()


@pytest.mark.integration
async def test_mark_read_foreign_notification_raises_404(db: None) -> None:
    user = await _make_user()
    other = await _make_user()
    try:
        note = await _make_notification(user, read=False)

        # User B cannot mark user A's notification — it is invisible to them.
        with pytest.raises(HTTPException) as exc:
            await mark_read(notification_id=note.id, user=other)
        assert exc.value.status_code == 404

        # And A's notification remains unread.
        refreshed = await Notification.get(id=note.id)
        assert refreshed.read_at is None
    finally:
        await Notification.filter(user_id=user.id).delete()
        await user.delete()
        await other.delete()


@pytest.mark.integration
async def test_mark_read_missing_notification_raises_404(db: None) -> None:
    user = await _make_user()
    try:
        with pytest.raises(HTTPException) as exc:
            await mark_read(notification_id=uuid.uuid4(), user=user)
        assert exc.value.status_code == 404
    finally:
        await user.delete()
