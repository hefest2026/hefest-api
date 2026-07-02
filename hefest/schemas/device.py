"""Device registration schemas for push-notification tokens."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hefest.models.device import DevicePlatform


class DeviceRegisterRequest(BaseModel):
    """Request schema for registering an Expo push token.

    Attributes:
        expo_push_token: The Expo push token reported by the device.
        platform: The device platform (``ios`` or ``android``).
    """

    expo_push_token: str = Field(min_length=1, max_length=255)
    platform: DevicePlatform


class DeviceUnregisterRequest(BaseModel):
    """Request schema for removing an Expo push token.

    Attributes:
        expo_push_token: The Expo push token to remove.
    """

    expo_push_token: str = Field(min_length=1, max_length=255)


class DeviceResponse(BaseModel):
    """Response schema for a registered device.

    Attributes:
        id: Device UUID.
        expo_push_token: The stored Expo push token.
        platform: The device platform.
        created_at: When the device was first registered.
        updated_at: When the registration was last refreshed.
    """

    id: UUID
    expo_push_token: str
    platform: DevicePlatform
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
