"""Authentication schemas for request/response validation."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, EmailStr
from pydantic.functional_validators import AfterValidator

from hefest.models.user import UserRole


def _password_validator(v: str) -> str:
    """Validate password field for Annotated type.

    Args:
        v: The password string to validate

    Returns:
        The password string if valid

    Raises:
        ValueError: If password is less than 12 characters
    """
    if len(v) < 12:
        raise ValueError("Password must be at least 12 characters long")
    return v


class RegisterRequest(BaseModel):
    """Request schema for user registration.

    Attributes:
        email: Valid email address for the user account
        password: Password (minimum 12 characters)
        full_name: User's full name
    """

    email: EmailStr
    password: Annotated[str, AfterValidator(_password_validator)]
    full_name: str


class LoginRequest(BaseModel):
    """Request schema for user login.

    Attributes:
        email: User's email address
        password: User's password
    """

    email: EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    """Request schema for email verification.

    Attributes:
        token: JWT token from email verification link
    """

    token: str


class TokenResponse(BaseModel):
    """Response schema for successful authentication.

    Attributes:
        access_token: JWT access token
        token_type: Token type (default: "bearer")
        expires_in: Token expiration time in seconds
        refresh_token: Opaque refresh token, returned only to mobile clients
            (``X-Client-Id: mobile_app``) for storage in the device keystore.
            ``None`` for web clients, which receive it via the httpOnly cookie.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str | None = None


class OAuthProviderInfo(BaseModel):
    """OAuth provider availability information.

    Attributes:
        name: Provider name (e.g., "google", "microsoft")
        available: Whether provider is configured and available
        login_url: URL to initiate OAuth login (None if unavailable)
    """

    name: str
    available: bool
    login_url: str | None


class ProvidersResponse(BaseModel):
    """Response schema for available authentication providers.

    Attributes:
        password: Password authentication availability
        providers: List of available OAuth providers
    """

    password: dict[str, bool]
    providers: list[OAuthProviderInfo]


def _full_name_validator(v: str) -> str:
    """Validate and normalize a display name: trimmed and non-empty."""
    trimmed = v.strip()
    if not trimmed:
        raise ValueError("Full name must not be empty")
    if len(trimmed) > 200:
        raise ValueError("Full name must be at most 200 characters long")
    return trimmed


class UserUpdateRequest(BaseModel):
    """Request schema for updating the current user's profile.

    Attributes:
        full_name: New display name (trimmed, non-empty, max 200 chars).
    """

    full_name: Annotated[str, AfterValidator(_full_name_validator)]


class ChangePasswordRequest(BaseModel):
    """Request schema for changing the current user's password.

    Attributes:
        current_password: The user's existing password, for re-authentication.
        new_password: The replacement password (minimum 12 characters).
    """

    current_password: str
    new_password: Annotated[str, AfterValidator(_password_validator)]


class UserMeResponse(BaseModel):
    """Response schema for the current authenticated user.

    Attributes:
        id: User UUID
        email: User email address
        full_name: User display name
        role: User role (student or organizer)
    """

    id: UUID
    email: str
    full_name: str
    role: UserRole

    model_config = {"from_attributes": True}
