"""Tests for profile self-service: PATCH /users/me and change-password.

Schema validation is exercised without a database; the endpoint behaviour
(name update, password re-verification, session revocation) runs against the
ephemeral testcontainers Postgres via the ``db`` fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from hefest.models.refresh_token import RefreshClient
from hefest.models.user import User, UserRole
from hefest.routers.auth import change_password, update_me
from hefest.schemas.auth import (
    ChangePasswordRequest,
    UserUpdateRequest,
)
from hefest.services import auth as auth_svc


class TestProfileSchemaValidation:
    def test_full_name_is_trimmed(self) -> None:
        assert UserUpdateRequest(full_name="  Petar  ").full_name == "Petar"

    def test_blank_full_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UserUpdateRequest(full_name="   ")

    def test_short_new_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="x", new_password="short")


@pytest.fixture()
async def user(db: None) -> User:
    """A verified organizer with a known password."""
    return await User.create(
        email=f"prof-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=auth_svc.hash_password("current-password-123"),
        full_name="Original Name",
        role=UserRole.organizer,
        email_verified_at=datetime.now(UTC),
    )


pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
async def test_update_me_changes_full_name(user: User) -> None:
    try:
        result = await update_me(UserUpdateRequest(full_name="New Name"), user)
        assert result.full_name == "New Name"
        refreshed = await User.get(id=user.id)
        assert refreshed.full_name == "New Name"
    finally:
        await user.delete()


@pytest.mark.integration
async def test_change_password_success_revokes_sessions(user: User) -> None:
    try:
        # two live refresh tokens for this user (web + mobile)
        await auth_svc.issue_refresh_token(user, client=RefreshClient.web)
        await auth_svc.issue_refresh_token(user, client=RefreshClient.mobile)

        await change_password(
            ChangePasswordRequest(
                current_password="current-password-123",
                new_password="brand-new-password-456",
            ),
            Response(),
            user,
        )

        refreshed = await User.get(id=user.id)
        assert refreshed.password_hash is not None
        assert auth_svc.verify_password(
            "brand-new-password-456", refreshed.password_hash
        )
        # every refresh token is revoked → other sessions can no longer refresh
        active = await user.refresh_tokens.filter(revoked_at=None).count()
        assert active == 0
    finally:
        await user.delete()


@pytest.mark.integration
async def test_change_password_wrong_current_rejected(user: User) -> None:
    try:
        with pytest.raises(HTTPException) as exc:
            await change_password(
                ChangePasswordRequest(
                    current_password="wrong-password-000",
                    new_password="brand-new-password-456",
                ),
                Response(),
                user,
            )
        assert exc.value.status_code == 401
        refreshed = await User.get(id=user.id)
        # password unchanged
        assert auth_svc.verify_password(
            "current-password-123", refreshed.password_hash or ""
        )
    finally:
        await user.delete()
