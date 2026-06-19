from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
from fastapi import Response
from passlib.context import CryptContext
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from hefest.config import settings
from hefest.models.oauth_identity import OAuthIdentity
from hefest.models.refresh_token import RefreshToken
from hefest.models.user import User, UserRole

if TYPE_CHECKING:
    from fastapi_sso.sso.base import OpenID

# ---------------------------------------------------------------------------
# Password hashing (S1)
# ---------------------------------------------------------------------------

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt.

    Args:
        plain: The plain-text password to hash.

    Returns:
        A bcrypt hash string.
    """
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash.

    Args:
        plain: The plain-text password to verify.
        hashed: The stored bcrypt hash.

    Returns:
        True if the password matches, False otherwise.
    """
    return _pwd_ctx.verify(plain, hashed)


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


async def issue_refresh_token(user: User) -> str:
    """Create and persist a new opaque refresh token; return the raw token.

    Args:
        user: The User instance to issue a token for.

    Returns:
        The raw (unhashed) refresh token string.
    """
    raw = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    await RefreshToken.create(
        user=user,
        token_hash=_hash_token(raw),
        expires_at=expires_at,
    )
    return raw


# ---------------------------------------------------------------------------
# Refresh token rotation (atomic + reuse detection)
# ---------------------------------------------------------------------------


async def rotate_refresh_token(raw_token: str) -> tuple[str, str]:
    """Rotate a refresh token. Returns (new_access_token, new_raw_refresh_token).

    Args:
        raw_token: The raw (unhashed) refresh token from the client.

    Returns:
        A tuple of (new_access_token, new_raw_refresh_token).

    Raises:
        ValueError: On invalid or expired token.
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

        # valid — rotate: revoke old, issue new pair
        record.revoked_at = now
        await record.save(update_fields=["revoked_at"])

        user = await User.get(id=record.user_id)  # ty: ignore[unresolved-attribute]
        new_access = create_access_token(user)
        new_refresh = await issue_refresh_token(user)

    return new_access, new_refresh


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
