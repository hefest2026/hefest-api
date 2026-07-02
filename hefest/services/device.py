"""Device registration service — Expo push-token storage."""

from __future__ import annotations

from hefest.models.device import Device, DevicePlatform
from hefest.models.user import User


async def register_device(
    user: User, expo_push_token: str, platform: DevicePlatform
) -> Device:
    """Register an Expo push token, binding it to the given user.

    Tokens are globally unique. If the token already exists — a reinstall, or a
    device that previously belonged to another account — it is re-bound to
    ``user`` and its platform refreshed; otherwise a new row is created.

    Args:
        user: The authenticated owner of the device.
        expo_push_token: The Expo push token reported by the device.
        platform: The device platform.

    Returns:
        The created or updated :class:`Device`.
    """
    device, _ = await Device.update_or_create(
        expo_push_token=expo_push_token,
        defaults={"user": user, "platform": platform},
    )
    return device


async def unregister_device(user: User, expo_push_token: str) -> bool:
    """Remove an Expo push token owned by the user.

    Scoped to ``user`` so a caller can only remove their own token; unknown or
    foreign tokens are a no-op (idempotent, no information leak).

    Args:
        user: The authenticated owner of the device.
        expo_push_token: The Expo push token to remove.

    Returns:
        ``True`` if a row was deleted, ``False`` otherwise.
    """
    deleted = await Device.filter(user=user, expo_push_token=expo_push_token).delete()
    return bool(deleted)
