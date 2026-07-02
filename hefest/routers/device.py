"""Device router — Expo push-token registration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from hefest.models.user import User
from hefest.routers.deps import get_current_user
from hefest.schemas.device import (
    DeviceRegisterRequest,
    DeviceResponse,
    DeviceUnregisterRequest,
)
from hefest.services import device as device_svc

router = APIRouter(tags=["devices"])


@router.post(
    "/devices/register",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_device(
    body: DeviceRegisterRequest,
    user: User = Depends(get_current_user),
) -> DeviceResponse:
    """Register (or re-bind) the caller's Expo push token."""
    device = await device_svc.register_device(user, body.expo_push_token, body.platform)
    return DeviceResponse.model_validate(device)


@router.post("/devices/unregister", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    body: DeviceUnregisterRequest,
    user: User = Depends(get_current_user),
) -> None:
    """Remove the caller's Expo push token (called on logout). Idempotent."""
    await device_svc.unregister_device(user, body.expo_push_token)
