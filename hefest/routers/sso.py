from __future__ import annotations

import secrets
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from hefest.config import SUPPORTED_OAUTH_PROVIDERS, settings
from hefest.models.refresh_token import RefreshClient
from hefest.schemas.auth import OAuthProviderInfo, ProvidersResponse
from hefest.services import auth as auth_svc

router = APIRouter(tags=["sso"])

# Login clients selectable via the ``?client=`` query param. The value is a
# closed enum — never an arbitrary redirect URL — so callers can only choose
# between the two server-configured targets (web page vs native deeplink).
LoginClient = Literal["web", "mobile"]

# Separator between the random CSRF nonce and the client tag inside the OAuth
# ``state`` parameter, e.g. ``"<nonce>:mobile"``.
_STATE_SEPARATOR = ":"


@router.get("/auth/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """List all supported auth providers and their availability."""
    enabled = settings.enabled_oauth_providers
    providers = [
        OAuthProviderInfo(
            name=p,
            available=p in enabled,
            login_url=f"/auth/{p}/login" if p in enabled else None,
        )
        for p in SUPPORTED_OAUTH_PROVIDERS
    ]
    return ProvidersResponse(password={"available": True}, providers=providers)


def _build_state(client: LoginClient) -> str:
    """Encode the login client into a fresh, unguessable OAuth ``state`` value.

    The random prefix is what fastapi-sso binds to the ``sso_state`` cookie for
    CSRF protection; the suffix rides along so the callback can recover which
    client (web vs mobile) started the flow.
    """
    return f"{secrets.token_urlsafe(16)}{_STATE_SEPARATOR}{client}"


def _client_from_state(state: str | None) -> RefreshClient:
    """Recover the refresh-token client from a callback ``state`` value.

    Defaults to ``web`` for any missing or unrecognised tag, so a malformed
    state can never escalate a flow into the mobile deeplink path.
    """
    if state and state.rsplit(_STATE_SEPARATOR, 1)[-1] == "mobile":
        return RefreshClient.mobile
    return RefreshClient.web


def _get_sso_client(provider: str) -> Any:  # type: ignore[return]
    """Return the SSO client for the given provider, or raise 404 if not enabled."""
    if provider not in settings.enabled_oauth_providers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{provider} SSO is not configured",
            headers={"X-Error-Code": "sso_provider_disabled"},
        )
    if provider == "google":
        from fastapi_sso.sso.google import GoogleSSO

        return GoogleSSO(
            settings.google_client_id,
            settings.google_client_secret,
            settings.google_redirect_uri,
            use_state=True,
        )
    if provider == "microsoft":
        from fastapi_sso.sso.microsoft import MicrosoftSSO

        return MicrosoftSSO(
            settings.microsoft_client_id,
            settings.microsoft_client_secret,
            tenant=settings.microsoft_tenant,
            redirect_uri=settings.microsoft_redirect_uri,
            use_state=True,
            scope=["openid", "email", "profile"],
        )


def _success_redirect(
    client: RefreshClient, access: str, refresh: str
) -> RedirectResponse:
    """Build the post-login redirect, delivering tokens per client type.

    Web clients land on the SPA success URL with the access token in the URL
    fragment and the refresh token in an httpOnly cookie. Native clients land on
    the app deeplink with *both* tokens in the fragment, because a
    system-browser-to-app handoff cannot share the cookie jar.
    """
    if client is RefreshClient.mobile:
        target = (
            f"{settings.mobile_oauth_success_url}"
            f"#access_token={access}&refresh_token={refresh}"
        )
        return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)

    redirect = RedirectResponse(
        url=f"{settings.frontend_oauth_success_url}#access_token={access}",
        status_code=status.HTTP_302_FOUND,
    )
    auth_svc.set_refresh_cookie(redirect, refresh)
    return redirect


async def _oauth_callback(
    request: Request, sso: Any, provider: str
) -> RedirectResponse:
    """Process OAuth callback, create/find user, issue token pair, redirect."""
    async with sso:
        openid = await sso.verify_and_process(request)
    # verify_and_process has already checked the returned state against the
    # ``sso_state`` cookie (CSRF), so the client tag is safe to trust here.
    client = _client_from_state(request.query_params.get("state"))
    user = await auth_svc.find_or_create_oauth_user(provider, openid)
    access = auth_svc.create_access_token(user)
    refresh = await auth_svc.issue_refresh_token(user, client=client)
    return _success_redirect(client, access, refresh)


@router.get("/auth/google/login")
async def google_login(client: LoginClient = "web") -> RedirectResponse:
    """Initiate Google OAuth login flow for the given client (web or mobile)."""
    sso = _get_sso_client("google")
    async with sso:
        return await sso.get_login_redirect(state=_build_state(client))


@router.get("/auth/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    """Handle Google OAuth callback."""
    return await _oauth_callback(request, _get_sso_client("google"), "google")


@router.get("/auth/microsoft/login")
async def microsoft_login(client: LoginClient = "web") -> RedirectResponse:
    """Initiate Microsoft OAuth login flow for the given client (web or mobile)."""
    sso = _get_sso_client("microsoft")
    async with sso:
        return await sso.get_login_redirect(state=_build_state(client))


@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request) -> RedirectResponse:
    """Handle Microsoft OAuth callback."""
    return await _oauth_callback(request, _get_sso_client("microsoft"), "microsoft")
