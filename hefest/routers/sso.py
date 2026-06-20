from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from hefest.config import SUPPORTED_OAUTH_PROVIDERS, settings
from hefest.schemas.auth import OAuthProviderInfo, ProvidersResponse
from hefest.services import auth as auth_svc

router = APIRouter(tags=["sso"])


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


async def _oauth_callback(
    request: Request, response: Response, sso: Any, provider: str
) -> RedirectResponse:
    """Process OAuth callback, create/find user, issue token pair."""
    async with sso:
        openid = await sso.verify_and_process(request)
    user = await auth_svc.find_or_create_oauth_user(provider, openid)
    access = auth_svc.create_access_token(user)
    refresh = await auth_svc.issue_refresh_token(user)
    auth_svc.set_refresh_cookie(response, refresh)
    return RedirectResponse(
        url=f"{settings.frontend_oauth_success_url}#access_token={access}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/auth/google/login")
async def google_login() -> RedirectResponse:
    """Initiate Google OAuth login flow."""
    sso = _get_sso_client("google")
    async with sso:
        return await sso.get_login_redirect()


@router.get("/auth/google/callback")
async def google_callback(request: Request, response: Response) -> RedirectResponse:
    """Handle Google OAuth callback."""
    return await _oauth_callback(request, response, _get_sso_client("google"), "google")


@router.get("/auth/microsoft/login")
async def microsoft_login() -> RedirectResponse:
    """Initiate Microsoft OAuth login flow."""
    sso = _get_sso_client("microsoft")
    async with sso:
        return await sso.get_login_redirect()


@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request, response: Response) -> RedirectResponse:
    """Handle Microsoft OAuth callback."""
    return await _oauth_callback(
        request, response, _get_sso_client("microsoft"), "microsoft"
    )
