from __future__ import annotations

from unittest.mock import MagicMock

import jwt

from hefest.config import settings
from hefest.services.auth import (
    _hash_token,
    create_access_token,
    create_email_verify_token,
    hash_password,
    user_id_from_access_token,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_and_verify_password(self) -> None:
        """verify_password returns True when plain matches the hash."""
        plain = "super-secret-123"
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True

    def test_verify_wrong_password(self) -> None:
        """verify_password returns False when plain does not match."""
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False


class TestAccessToken:
    def test_create_access_token_payload(self) -> None:
        """Decoded access token contains the expected claims."""
        user = MagicMock()
        user.id = "00000000-0000-0000-0000-000000000001"
        user.role = "student"

        token = create_access_token(user)
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
        )

        assert payload["sub"] == str(user.id)
        assert payload["role"] == "student"
        assert payload["aud"] == settings.jwt_audience
        assert payload["type"] == "access"
        assert payload["iss"] == "hefest"


class TestEmailVerifyToken:
    def test_create_email_verify_token_payload(self) -> None:
        """Decoded email-verify token has correct aud and type claims."""
        user = MagicMock()
        user.id = "00000000-0000-0000-0000-000000000002"

        token = create_email_verify_token(user)
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="hefest-verify",
        )

        assert payload["sub"] == str(user.id)
        assert payload["aud"] == "hefest-verify"
        assert payload["type"] == "email_verify"


class TestUserIdFromAccessToken:
    def test_valid_access_token_returns_sub(self) -> None:
        """A valid access token yields its sub user_id."""
        user = MagicMock()
        user.id = "00000000-0000-0000-0000-000000000003"
        user.role = "student"

        assert user_id_from_access_token(create_access_token(user)) == str(user.id)

    def test_wrong_token_type_returns_none(self) -> None:
        """A non-access token (e.g. email-verify) is rejected."""
        user = MagicMock()
        user.id = "00000000-0000-0000-0000-000000000004"

        assert user_id_from_access_token(create_email_verify_token(user)) is None

    def test_garbage_token_returns_none(self) -> None:
        """An undecodable token returns None instead of raising."""
        assert user_id_from_access_token("not-a-jwt") is None


class TestHashToken:
    def test_hash_token_is_deterministic(self) -> None:
        """_hash_token returns the same digest for the same input."""
        x = "some-opaque-token-value"
        y = "a-different-token-value"
        assert _hash_token(x) == _hash_token(x)
        assert _hash_token(x) != _hash_token(y)
