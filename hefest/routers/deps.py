from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from hefest.config import settings
from hefest.models.refresh_token import RefreshClient
from hefest.models.user import User, UserRole

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

WEB_CLIENT_ID: Final = "web_app"
MOBILE_CLIENT_ID: Final = "mobile_app"
_CLIENT_IDS: Final[dict[str, RefreshClient]] = {
    WEB_CLIENT_ID: RefreshClient.web,
    MOBILE_CLIENT_ID: RefreshClient.mobile,
}


def get_refresh_client(
    x_client_id: Annotated[str | None, Header()] = None,
) -> RefreshClient:
    """Resolve the ``X-Client-Id`` request header to a :class:`RefreshClient`.

    Unknown or absent values default to ``web`` so existing browser clients —
    which never send the header — keep the httpOnly-cookie delivery path.

    Args:
        x_client_id: Value of the ``X-Client-Id`` header, if present.

    Returns:
        The resolved client (``web`` by default, ``mobile`` for ``mobile_app``).
    """
    return _CLIENT_IDS.get(x_client_id or "", RefreshClient.web)


async def get_current_user(token: str = Depends(_oauth2_scheme)) -> User:
    """Decode the Bearer JWT and return the authenticated User."""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
        )
    except jwt.PyJWTError:
        raise credentials_exc

    if payload.get("type") != "access":
        raise credentials_exc

    user_id: str | None = payload.get("sub")
    if user_id is None:
        raise credentials_exc

    user = await User.get_or_none(id=user_id)
    if user is None:
        raise credentials_exc

    return user


def require_role(*roles: UserRole) -> Callable[..., Awaitable[User]]:
    """Return a dependency that enforces the user has one of the given roles."""

    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient permissions",
            )
        return user

    return _check
