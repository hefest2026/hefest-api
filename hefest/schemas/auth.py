"""Authentication schemas for request/response validation."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, EmailStr
from pydantic.functional_validators import AfterValidator


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
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


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
