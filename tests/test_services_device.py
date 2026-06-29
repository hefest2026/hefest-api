"""Unit tests for hefest.services.device.

All Tortoise ORM calls are mocked so no database is required.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from hefest.models.device import DevicePlatform
from hefest.services import device as svc


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


class TestRegisterDevice:
    async def test_register_upserts_and_returns_device(self) -> None:
        """register_device upserts by token and returns the device."""
        user = _user()
        device = MagicMock()
        with patch.object(
            svc.Device,
            "update_or_create",
            new=AsyncMock(return_value=(device, True)),
        ) as uoc:
            result = await svc.register_device(
                user, "ExponentPushToken[abc]", DevicePlatform.ios
            )

        assert result is device
        uoc.assert_awaited_once_with(
            expo_push_token="ExponentPushToken[abc]",
            defaults={"user": user, "platform": DevicePlatform.ios},
        )


class TestUnregisterDevice:
    async def test_unregister_returns_true_when_deleted(self) -> None:
        """unregister_device returns True when a row was removed."""
        user = _user()
        qs = MagicMock()
        qs.delete = AsyncMock(return_value=1)
        with patch.object(svc.Device, "filter", return_value=qs) as flt:
            result = await svc.unregister_device(user, "tok")

        assert result is True
        flt.assert_called_once_with(user=user, expo_push_token="tok")

    async def test_unregister_returns_false_when_absent(self) -> None:
        """unregister_device is a no-op (False) for unknown/foreign tokens."""
        user = _user()
        qs = MagicMock()
        qs.delete = AsyncMock(return_value=0)
        with patch.object(svc.Device, "filter", return_value=qs):
            result = await svc.unregister_device(user, "tok")

        assert result is False
