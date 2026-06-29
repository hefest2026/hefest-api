"""Unit tests for the mobile refresh-token client binding (HEF-44).

Covers the X-Client-Id resolution, the rotation cross-check that stops a
cookie-bound web token from being delivered as a mobile body token, and the
router helper that enforces per-client delivery. All ORM/transaction calls are
mocked so no database is required.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hefest.models.refresh_token import RefreshClient
from hefest.routers import auth as auth_router
from hefest.routers import deps
from hefest.services import auth as svc


@asynccontextmanager
async def _mock_tx() -> AsyncIterator[None]:
    """No-op async context manager that replaces in_transaction()."""
    yield None


def _record(client: RefreshClient) -> MagicMock:
    rec = MagicMock()
    rec.client = client
    rec.revoked_at = None
    rec.expires_at = datetime.now(UTC) + timedelta(days=1)
    rec.user_id = uuid.uuid4()
    rec.save = AsyncMock()
    return rec


class TestGetRefreshClient:
    def test_mobile_header_maps_to_mobile(self) -> None:
        assert deps.get_refresh_client("mobile_app") is RefreshClient.mobile

    def test_web_header_maps_to_web(self) -> None:
        assert deps.get_refresh_client("web_app") is RefreshClient.web

    def test_missing_header_defaults_to_web(self) -> None:
        assert deps.get_refresh_client(None) is RefreshClient.web

    def test_unknown_header_defaults_to_web(self) -> None:
        assert deps.get_refresh_client("garbage") is RefreshClient.web


class TestRotateClientBinding:
    async def test_client_mismatch_is_rejected(self) -> None:
        """A web-bound token presented by a mobile client is refused."""
        rec = _record(RefreshClient.web)
        qs = MagicMock()
        qs.get_or_none = AsyncMock(return_value=rec)
        with (
            patch("hefest.services.auth.in_transaction", _mock_tx),
            patch.object(svc.RefreshToken, "select_for_update", return_value=qs),
        ):
            with pytest.raises(ValueError, match="client mismatch"):
                await svc.rotate_refresh_token("raw", RefreshClient.mobile)

        # the existing token must NOT be rotated/revoked on a mismatch
        rec.save.assert_not_awaited()

    async def test_success_preserves_bound_client(self) -> None:
        """A matching rotation issues the new token bound to the same client."""
        rec = _record(RefreshClient.mobile)
        qs = MagicMock()
        qs.get_or_none = AsyncMock(return_value=rec)
        user = MagicMock()
        with (
            patch("hefest.services.auth.in_transaction", _mock_tx),
            patch.object(svc.RefreshToken, "select_for_update", return_value=qs),
            patch.object(svc.User, "get", new=AsyncMock(return_value=user)),
            patch.object(svc, "create_access_token", return_value="access-x"),
            patch.object(
                svc, "issue_refresh_token", new=AsyncMock(return_value="refresh-x")
            ) as issue,
        ):
            access, refresh, client = await svc.rotate_refresh_token(
                "raw", RefreshClient.mobile
            )

        assert (access, refresh, client) == (
            "access-x",
            "refresh-x",
            RefreshClient.mobile,
        )
        issue.assert_awaited_once_with(user, client=RefreshClient.mobile)
        assert rec.revoked_at is not None


class TestTokenResponseDelivery:
    def test_mobile_returns_body_token_and_no_cookie(self) -> None:
        """Mobile clients get the refresh token in the body, never a cookie."""
        response = MagicMock()
        with patch.object(auth_router.auth_svc, "set_refresh_cookie") as set_cookie:
            tr = auth_router._token_response(
                response, "acc", "ref", RefreshClient.mobile
            )

        assert tr.refresh_token == "ref"
        set_cookie.assert_not_called()

    def test_web_sets_cookie_and_omits_body_token(self) -> None:
        """Web clients get the httpOnly cookie and a null body refresh token."""
        response = MagicMock()
        with patch.object(auth_router.auth_svc, "set_refresh_cookie") as set_cookie:
            tr = auth_router._token_response(response, "acc", "ref", RefreshClient.web)

        assert tr.refresh_token is None
        set_cookie.assert_called_once_with(response, "ref")
