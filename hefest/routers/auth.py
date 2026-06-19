from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from hefest.config import SUPPORTED_OAUTH_PROVIDERS, settings
from hefest.models.user import User, UserRole
from hefest.routers.deps import get_current_user
from hefest.schemas.auth import (
    LoginRequest,
    OAuthProviderInfo,
    ProvidersResponse,
    RegisterRequest,
    TokenResponse,
    VerifyEmailRequest,
)
from hefest.services import auth as auth_svc

router = APIRouter(tags=["auth"])


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


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest) -> dict[str, str]:
    """Register a new unverified student account."""
    from hefest.models.user import User

    if await User.exists(email=body.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
            headers={"X-Error-Code": "email_exists"},
        )
    user = await User.create(
        email=body.email,
        password_hash=auth_svc.hash_password(body.password),
        full_name=body.full_name,
        role=UserRole.student,
        email_verified_at=None,
    )
    # TODO: enqueue verification email via notification pipeline (outbox)
    verify_token = auth_svc.create_email_verify_token(user)
    response_body: dict[str, str] = {
        "message": "registered; check your email to verify your account"
    }
    if settings.env == "dev":
        response_body["verify_token"] = verify_token
    return response_body


@router.post("/auth/verify-email")
async def verify_email(body: VerifyEmailRequest, response: Response) -> TokenResponse:
    """Verify email address and activate the account; issue tokens."""
    try:
        user = await auth_svc.consume_email_verify_token(body.token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
            headers={"X-Error-Code": "invalid_verify_token"},
        )
    access = auth_svc.create_access_token(user)
    refresh = await auth_svc.issue_refresh_token(user)
    auth_svc.set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response) -> TokenResponse:
    """Authenticate with email + password; issue token pair."""
    user = await User.get_or_none(email=body.email)
    if (
        user is None
        or user.password_hash is None
        or not auth_svc.verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"X-Error-Code": "invalid_credentials"},
        )
    if user.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email not verified",
            headers={"X-Error-Code": "email_not_verified"},
        )
    access = auth_svc.create_access_token(user)
    refresh = await auth_svc.issue_refresh_token(user)
    auth_svc.set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access, expires_in=settings.jwt_expire_minutes * 60
    )


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_tokens(
    response: Response,
    cookie_token: Annotated[str | None, Cookie(alias="hefest_refresh")] = None,
    body: dict[str, str] | None = None,  # for future mobile Bearer mode
) -> TokenResponse:
    """Rotate the refresh token; issue a new token pair."""
    raw = cookie_token
    if raw is None and body:
        raw = body.get("refresh_token")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token required",
        )
    try:
        new_access, new_refresh = await auth_svc.rotate_refresh_token(raw)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token revoked",
            headers={"X-Error-Code": "token_reuse_detected"},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )
    auth_svc.set_refresh_cookie(response, new_refresh)
    return TokenResponse(
        access_token=new_access, expires_in=settings.jwt_expire_minutes * 60
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    cookie_token: Annotated[str | None, Cookie(alias="hefest_refresh")] = None,
) -> None:
    """Revoke the current refresh token and clear the cookie."""
    if cookie_token:
        await auth_svc.revoke_refresh_token(cookie_token)
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path="/auth",
    )


@router.post("/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> None:
    """Revoke all refresh tokens for the current user."""
    await auth_svc.revoke_all_for_user(str(current_user.id))
    response.delete_cookie(key=settings.refresh_cookie_name, path="/auth")


def _get_sso_client(provider: str):  # type: ignore[return]
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
