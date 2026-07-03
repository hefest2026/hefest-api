"""Unit tests for SSO web-vs-mobile client routing (no DB required).

Covers the OAuth ``state`` client tag round-trip, per-client redirect/token
delivery, and that the callback issues the correctly-bound refresh token and
lands the user on the right target (web success page vs native deeplink).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hefest.models.refresh_token import RefreshClient
from hefest.routers import sso


class _FakeSSO:
    """Stand-in for a fastapi-sso client: an async CM used as ``async with sso``
    whose ``verify_and_process`` yields a fixed OpenID (mirrors real usage where
    the client is referenced by name inside the ``with`` block)."""

    def __init__(self, openid: object) -> None:
        self._openid = openid

    async def __aenter__(self) -> _FakeSSO:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def verify_and_process(self, request: object) -> object:
        return self._openid


def test_state_roundtrip_recovers_client() -> None:
    for client in ("web", "mobile"):
        state = sso._build_state(client)  # type: ignore[arg-type]
        nonce, tag = state.rsplit(":", 1)
        assert tag == client
        assert len(nonce) >= 16  # unguessable CSRF nonce
        assert sso._client_from_state(state) is (
            RefreshClient.mobile if client == "mobile" else RefreshClient.web
        )


@pytest.mark.parametrize("state", [None, "", "no-separator", "nonce:desktop", "nonce:"])
def test_malformed_state_defaults_to_web(state: str | None) -> None:
    assert sso._client_from_state(state) is RefreshClient.web


def test_success_redirect_mobile_puts_both_tokens_in_fragment() -> None:
    with patch.object(
        sso.settings, "mobile_oauth_success_url", "hefestmobile://auth/callback"
    ):
        redirect = sso._success_redirect(RefreshClient.mobile, "ACCESS", "REFRESH")

    location = redirect.headers["location"]
    assert (
        location
        == "hefestmobile://auth/callback#access_token=ACCESS&refresh_token=REFRESH"
    )
    # Native handoff cannot use cookies — nothing is set.
    assert "set-cookie" not in {k.lower() for k in redirect.headers}


def test_success_redirect_web_uses_cookie_and_omits_refresh_from_url() -> None:
    with (
        patch.object(
            sso.settings, "frontend_oauth_success_url", "https://app.example/cb"
        ),
        patch.object(sso.settings, "refresh_cookie_name", "hefest_refresh"),
    ):
        redirect = sso._success_redirect(RefreshClient.web, "ACCESS", "REFRESH")

    location = redirect.headers["location"]
    assert location == "https://app.example/cb#access_token=ACCESS"
    assert "REFRESH" not in location
    set_cookie = redirect.headers.get("set-cookie", "")
    assert "hefest_refresh=REFRESH" in set_cookie


@pytest.mark.parametrize(
    ("state", "expected_client"),
    [("nonce:mobile", RefreshClient.mobile), ("nonce:web", RefreshClient.web)],
)
async def test_callback_issues_bound_token_for_state_client(
    state: str, expected_client: RefreshClient
) -> None:
    request = MagicMock()
    request.query_params = {"state": state}
    sso_client = _FakeSSO(SimpleNamespace(email="u@x.io"))
    user = SimpleNamespace(id="uid")

    with (
        patch.object(sso, "_success_redirect") as success,
        patch.object(
            sso.auth_svc, "find_or_create_oauth_user", AsyncMock(return_value=user)
        ),
        patch.object(sso.auth_svc, "create_access_token", return_value="ACCESS"),
        patch.object(
            sso.auth_svc, "issue_refresh_token", AsyncMock(return_value="REFRESH")
        ) as issue,
    ):
        await sso._oauth_callback(request, sso_client, "google")

    issue.assert_awaited_once_with(user, client=expected_client)
    success.assert_called_once_with(expected_client, "ACCESS", "REFRESH")
