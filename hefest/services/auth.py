from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import bcrypt
import jwt
from fastapi import Response
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from hefest.config import settings
from hefest.models.oauth_identity import OAuthIdentity
from hefest.models.refresh_token import RefreshClient, RefreshToken
from hefest.models.user import User, UserRole

if TYPE_CHECKING:
    from fastapi_sso.sso.base import OpenID

# ---------------------------------------------------------------------------
# Password hashing (S1)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt (cost 12)."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Access token
# ---------------------------------------------------------------------------


def create_access_token(user: User) -> str:
    """Create a short-lived HS256 JWT access token.

    Args:
        user: The User instance to encode into the token.

    Returns:
        A signed JWT string.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iss": "hefest",
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# ---------------------------------------------------------------------------
# Email verification token
# ---------------------------------------------------------------------------


def create_email_verify_token(user: User) -> str:
    """Create a short-lived JWT to embed in a verification link.

    Args:
        user: The User instance to create a verification token for.

    Returns:
        A signed JWT string.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "aud": "hefest-verify",
        "exp": now + timedelta(hours=settings.email_verify_expire_hours),
        "type": "email_verify",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def build_email_verify_link(user: User) -> str:
    """Build the full verification link to embed in a verification email.

    Mints a fresh stateless verification JWT for ``user`` and appends it to the
    configured ``email_verify_url`` as a ``token`` query parameter. Because the
    token is stateless, the delivery worker can call this at send time without
    any shared state with the API process that created the account.

    Args:
        user: The User the verification link is for.

    Returns:
        An absolute URL such as ``https://app/verify-email?token=<jwt>``.
    """
    token = create_email_verify_token(user)
    separator = "&" if "?" in settings.email_verify_url else "?"
    return f"{settings.email_verify_url}{separator}token={token}"


async def consume_email_verify_token(token: str) -> User:
    """Decode and consume an email verification token; set email_verified_at.

    Args:
        token: The raw JWT verification token.

    Returns:
        The verified User instance.

    Raises:
        ValueError: If the token is invalid, expired, wrong type, or user not found.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="hefest-verify",
        )
    except jwt.PyJWTError as exc:
        raise ValueError("invalid or expired verification token") from exc

    if payload.get("type") != "email_verify":
        raise ValueError("wrong token type")

    user = await User.get_or_none(id=payload["sub"])
    if user is None:
        raise ValueError("user not found")

    # idempotent: only update if not already verified
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
        await user.save(update_fields=["email_verified_at"])
    return user


# ---------------------------------------------------------------------------
# Refresh token issuance
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    """Compute a SHA-256 hex digest of a token string.

    Args:
        token: The raw token string to hash.

    Returns:
        A 64-character hex string.
    """
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_refresh_token(
    user: User, client: RefreshClient = RefreshClient.web
) -> str:
    """Create and persist a new opaque refresh token; return the raw token.

    Args:
        user: The User instance to issue a token for.
        client: The client the token is bound to; fixes how it is later
            delivered on rotation (cookie for ``web``, body for ``mobile``).

    Returns:
        The raw (unhashed) refresh token string.
    """
    raw = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    await RefreshToken.create(
        user=user,
        token_hash=_hash_token(raw),
        client=client,
        expires_at=expires_at,
    )
    return raw


# ---------------------------------------------------------------------------
# Refresh token rotation (atomic + reuse detection)
# ---------------------------------------------------------------------------


async def rotate_refresh_token(
    raw_token: str, presented_client: RefreshClient
) -> tuple[str, str, RefreshClient]:
    """Rotate a refresh token, preserving its bound client.

    Args:
        raw_token: The raw (unhashed) refresh token from the client.
        presented_client: The client making the request (from ``X-Client-Id``).
            Must match the client the token was issued to; this stops a
            cookie-bound ``web`` token from being rotated into a ``mobile``
            body response (and vice-versa).

    Returns:
        A tuple of (new_access_token, new_raw_refresh_token, bound_client).

    Raises:
        ValueError: On invalid, expired, or client-mismatched token.
        PermissionError: On reuse detection (revokes whole family).
    """
    token_hash = _hash_token(raw_token)

    async with in_transaction():
        record = await RefreshToken.select_for_update().get_or_none(
            token_hash=token_hash
        )

        if record is None:
            raise ValueError("refresh token not found")

        now = datetime.now(UTC)

        if record.expires_at < now and record.revoked_at is None:
            # expired but not yet revoked — treat as invalid
            raise ValueError("refresh token expired")

        if record.revoked_at is not None:
            # already revoked → reuse detected → revoke entire family
            # Tortoise exposes FK id via `user_id` at runtime; ty doesn't model it
            await RefreshToken.filter(
                user_id=record.user_id,  # ty: ignore[unresolved-attribute]
                revoked_at=None,
            ).update(revoked_at=now)
            raise PermissionError("token_reuse_detected")

        if record.client != presented_client:
            # delivery channel is bound at issuance — refuse cross-client use
            raise ValueError("client mismatch")

        # valid — rotate: revoke old, issue new pair bound to the same client
        record.revoked_at = now
        await record.save(update_fields=["revoked_at"])

        user = await User.get(id=record.user_id)  # ty: ignore[unresolved-attribute]
        new_access = create_access_token(user)
        new_refresh = await issue_refresh_token(user, client=record.client)

    return new_access, new_refresh, record.client


# ---------------------------------------------------------------------------
# Revoke single token
# ---------------------------------------------------------------------------


async def revoke_refresh_token(raw_token: str) -> None:
    """Revoke a single refresh token (logout).

    Args:
        raw_token: The raw (unhashed) refresh token to revoke.
    """
    token_hash = _hash_token(raw_token)
    await RefreshToken.filter(token_hash=token_hash, revoked_at=None).update(
        revoked_at=datetime.now(UTC)
    )


# ---------------------------------------------------------------------------
# Revoke all tokens for a user
# ---------------------------------------------------------------------------


async def revoke_all_for_user(user_id: str) -> None:
    """Revoke all active refresh tokens for a user (logout-all).

    Args:
        user_id: The string UUID of the user whose tokens to revoke.
    """
    await RefreshToken.filter(user_id=user_id, revoked_at=None).update(
        revoked_at=datetime.now(UTC)
    )


# ---------------------------------------------------------------------------
# Authentication helpers for logout-all (cookie or Bearer)
# ---------------------------------------------------------------------------


def user_id_from_access_token(token: str) -> str | None:
    """Return the subject user_id of a valid access JWT, or None if invalid.

    Args:
        token: The raw Bearer access token.

    Returns:
        The user_id string from the ``sub`` claim, or None if the token is
        invalid, expired, or not an access token.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload.get("sub")


async def user_id_for_active_refresh_token(raw_token: str) -> str | None:
    """Return the owner user_id of an active (unrevoked, unexpired) refresh token.

    Args:
        raw_token: The raw (unhashed) refresh token from the client.

    Returns:
        The owning user_id string, or None if the token is unknown, revoked,
        or expired.
    """
    record = await RefreshToken.get_or_none(
        token_hash=_hash_token(raw_token), revoked_at=None
    )
    if record is None or record.expires_at < datetime.now(UTC):
        return None
    return str(record.user_id)  # ty: ignore[unresolved-attribute]


# ---------------------------------------------------------------------------
# Set refresh cookie
# ---------------------------------------------------------------------------


def set_refresh_cookie(response: Response, raw_token: str) -> None:
    """Write the refresh token into an httpOnly cookie on the response.

    Args:
        response: The FastAPI Response object to set the cookie on.
        raw_token: The raw (unhashed) refresh token value.
    """
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=raw_token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="strict",
        path="/auth",
        max_age=settings.refresh_token_expire_days * 86400,
    )


# ---------------------------------------------------------------------------
# Find or create OAuth user
# ---------------------------------------------------------------------------


async def find_or_create_oauth_user(provider: str, openid: OpenID) -> User:
    """Find or create a User from an OAuth OpenID response.

    Implements account-linking and takeover logic:
    1. If an OAuthIdentity exists for this provider+subject, return its user.
    2. If a User exists with the same email and is verified, auto-link.
    3. If a User exists with the same email but unverified, take over.
    4. Otherwise, create a new User and OAuthIdentity.

    Uses a transaction with an IntegrityError backstop for concurrency.

    Args:
        provider: The OAuth provider name (e.g. 'google', 'microsoft').
        openid: The OpenID response from the SSO provider.

    Returns:
        The found or created User instance.

    Raises:
        IntegrityError: Handled internally; triggers a retry via _inner().
    """

    async def _inner() -> User:
        # 1. Identity exists → return its user; refresh email if changed
        identity = await OAuthIdentity.get_or_none(
            provider=provider, subject=openid.id
        ).select_related("user")
        if identity is not None:
            if identity.email != openid.email:
                identity.email = openid.email
                await identity.save(update_fields=["email"])
            return identity.user

        # 2. Look up by email
        existing = await User.get_or_none(email=openid.email)
        now = datetime.now(UTC)

        if existing is not None:
            if existing.email_verified_at is not None:
                # Auto-link: verified local account
                await OAuthIdentity.create(
                    user=existing,
                    provider=provider,
                    subject=openid.id,
                    email=openid.email,
                )
                return existing
            else:
                # Take over dormant unverified row
                existing.email_verified_at = now
                existing.password_hash = None  # ty: ignore[invalid-assignment]
                existing.full_name = openid.display_name or existing.full_name
                await existing.save(
                    update_fields=["email_verified_at", "password_hash", "full_name"]
                )
                await OAuthIdentity.create(
                    user=existing,
                    provider=provider,
                    subject=openid.id,
                    email=openid.email,
                )
                return existing

        # 3. New user
        user = await User.create(
            email=openid.email,
            full_name=openid.display_name or "",
            password_hash=None,
            role=UserRole.student,
            email_verified_at=now,
        )
        await OAuthIdentity.create(
            user=user,
            provider=provider,
            subject=openid.id,
            email=openid.email,
        )
        return user

    async with in_transaction():
        try:
            return await _inner()
        except IntegrityError:
            # Concurrent registration — re-fetch and re-evaluate
            return await _inner()
