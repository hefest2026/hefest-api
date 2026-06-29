from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response, status
from tortoise.transactions import in_transaction

from hefest.config import settings
from hefest.models.notification_job import NotificationJob
from hefest.models.refresh_token import RefreshClient
from hefest.models.user import User, UserRole
from hefest.routers.deps import get_current_user, get_refresh_client
from hefest.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserMeResponse,
    VerifyEmailRequest,
)
from hefest.services import auth as auth_svc

router = APIRouter(tags=["auth"])


def _token_response(
    response: Response, access: str, refresh: str, client: RefreshClient
) -> TokenResponse:
    """Build a token response, delivering the refresh token per bound client.

    Web clients receive the refresh token only via the httpOnly cookie; mobile
    clients receive it only in the response body for the device keystore.

    Args:
        response: The FastAPI response (used to set the web cookie).
        access: The freshly minted access token.
        refresh: The raw refresh token to deliver.
        client: The client the refresh token is bound to.

    Returns:
        The populated :class:`TokenResponse`.
    """
    body_refresh: str | None = None
    if client is RefreshClient.mobile:
        body_refresh = refresh
    else:
        auth_svc.set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=settings.jwt_expire_minutes * 60,
        refresh_token=body_refresh,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest) -> dict[str, str]:
    """Register a new unverified student account and enqueue its verify email."""
    if await User.exists(email=body.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
            headers={"X-Error-Code": "email_exists"},
        )
    # User row and its verification outbox job are written in one transaction so
    # the AFTER INSERT NOTIFY fires at COMMIT and the account can never exist
    # without its pending verification email. The job is account-scoped: no
    # event, payload carries only the student id (worker mints the token).
    async with in_transaction("default") as conn:
        user = await User.create(
            email=body.email,
            password_hash=auth_svc.hash_password(body.password),
            full_name=body.full_name,
            role=UserRole.student,
            email_verified_at=None,
            using_db=conn,
        )
        await NotificationJob.create(
            event=None,
            event_type="EmailVerify",
            payload={"student_id": str(user.id)},
            idempotency_key=f"{user.id}:EmailVerify",
            using_db=conn,
        )
    response_body: dict[str, str] = {
        "message": "registered; check your email to verify your account"
    }
    if settings.env == "dev":
        response_body["verify_token"] = auth_svc.create_email_verify_token(user)
    return response_body


@router.post("/auth/verify-email")
async def verify_email(
    body: VerifyEmailRequest,
    response: Response,
    client: RefreshClient = Depends(get_refresh_client),
) -> TokenResponse:
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
    refresh = await auth_svc.issue_refresh_token(user, client=client)
    return _token_response(response, access, refresh, client)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    client: RefreshClient = Depends(get_refresh_client),
) -> TokenResponse:
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
    refresh = await auth_svc.issue_refresh_token(user, client=client)
    return _token_response(response, access, refresh, client)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_tokens(
    response: Response,
    client: RefreshClient = Depends(get_refresh_client),
    cookie_token: Annotated[str | None, Cookie(alias="hefest_refresh")] = None,
    payload: dict[str, str] | None = None,  # mobile sends {"refresh_token": ...}
) -> TokenResponse:
    """Rotate the refresh token; issue a new token pair."""
    raw = cookie_token
    if raw is None and payload:
        raw = payload.get("refresh_token")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token required",
        )
    try:
        new_access, new_refresh, bound_client = await auth_svc.rotate_refresh_token(
            raw, client
        )
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
    return _token_response(response, new_access, new_refresh, bound_client)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    cookie_token: Annotated[str | None, Cookie(alias="hefest_refresh")] = None,
    payload: dict[str, str] | None = None,  # mobile sends {"refresh_token": ...}
) -> None:
    """Revoke the current refresh token and clear the cookie.

    Accepts the token from the ``hefest_refresh`` cookie (web) or a
    ``{"refresh_token": ...}`` body (mobile), so native clients can revoke the
    token they hold in their keystore.
    """
    raw = cookie_token
    if raw is None and payload:
        raw = payload.get("refresh_token")
    if raw:
        await auth_svc.revoke_refresh_token(raw)
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path="/auth",
    )


@router.get("/users/me", response_model=UserMeResponse)
async def get_me(user: User = Depends(get_current_user)) -> UserMeResponse:
    """Return the profile of the currently authenticated user."""
    return UserMeResponse.model_validate(user)


@router.post("/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    cookie_token: Annotated[str | None, Cookie(alias="hefest_refresh")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Revoke all refresh tokens for the current user.

    Authenticates via the ``hefest_refresh`` cookie (browser sessions) or an
    ``Authorization: Bearer`` access token (mobile/API clients). The cookie is
    preferred so a browser holding only the httpOnly refresh cookie can log out
    of every device — mirroring ``/auth/logout``.
    """
    user_id: str | None = None
    if cookie_token:
        user_id = await auth_svc.user_id_for_active_refresh_token(cookie_token)
    if user_id is None and authorization and authorization.startswith("Bearer "):
        user_id = auth_svc.user_id_from_access_token(authorization[7:])
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await auth_svc.revoke_all_for_user(user_id)
    response.delete_cookie(key=settings.refresh_cookie_name, path="/auth")
