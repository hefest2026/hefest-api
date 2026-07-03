"""Expo push delivery — best-effort side channel alongside the email send.

Unlike the mailer, a push failure must never affect the outbox job's
completion/retry state: the job's at-least-once contract exists to guarantee
the *email* is delivered, and retrying a job to fix a push hiccup would resend
that email. So ``Pusher.send`` never raises — every failure (transport,
timeout, or a per-token error from Expo) is logged and swallowed here.

Invalid tokens (``DeviceNotRegistered``) are pruned from the ``devices`` table
so the worker stops retrying them on every future job for that user.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from hefest.models.device import Device
from hefest.worker.push_templates import PushContent

logger = logging.getLogger(__name__)

_MAX_TOKENS_PER_REQUEST = 100
"""Expo's documented cap on messages per push-send request."""


class _PusherConfig(Protocol):
    """Structural interface for the settings consumed by ``Pusher``."""

    expo_push_url: str
    expo_access_token: str
    expo_push_timeout: int


class Pusher:
    """Sends Expo push notifications to every device token owned by a user."""

    def __init__(self, settings: _PusherConfig) -> None:
        """Create the underlying HTTP client. No I/O yet.

        Args:
            settings: Runtime config; reads ``expo_push_*``. Any object
                satisfying ``_PusherConfig`` is accepted.
        """
        self._url = settings.expo_push_url
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.expo_access_token:
            headers["Authorization"] = f"Bearer {settings.expo_access_token}"
        self._client = httpx.AsyncClient(
            headers=headers, timeout=settings.expo_push_timeout
        )

    async def send_to_tokens(
        self, tokens: list[str], content: PushContent, data: dict[str, Any]
    ) -> None:
        """Push ``content`` to every token, pruning any that are dead.

        Best-effort: never raises. Transport failures and per-token Expo
        errors are logged and otherwise ignored, except ``DeviceNotRegistered``
        tokens, which are deleted from the ``devices`` table.

        Args:
            tokens: Recipient Expo push tokens (already deduplicated per job).
            content: Rendered title/body.
            data: Extra payload delivered to the app (e.g. ``{"event_id": ..}``).
        """
        if not tokens:
            return

        for start in range(0, len(tokens), _MAX_TOKENS_PER_REQUEST):
            batch = tokens[start : start + _MAX_TOKENS_PER_REQUEST]
            await self._send_batch(batch, content, data)

    async def _send_batch(
        self, tokens: list[str], content: PushContent, data: dict[str, Any]
    ) -> None:
        messages = [
            {
                "to": token,
                "title": content.title,
                "body": content.body,
                "data": data,
            }
            for token in tokens
        ]
        try:
            response = await self._client.post(self._url, json=messages)
            response.raise_for_status()
            tickets = response.json().get("data", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Expo push send failed for %d token(s): %s", len(tokens), exc
            )
            return

        dead_tokens = [
            token
            for token, ticket in zip(tokens, tickets, strict=False)
            if _is_unregistered(ticket)
        ]
        for token, ticket in zip(tokens, tickets, strict=False):
            if ticket.get("status") == "error" and not _is_unregistered(ticket):
                logger.warning("Expo push error for token %s: %s", token, ticket)

        if dead_tokens:
            await Device.filter(expo_push_token__in=dead_tokens).delete()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


def _is_unregistered(ticket: dict[str, Any]) -> bool:
    return (
        ticket.get("status") == "error"
        and ticket.get("details", {}).get("error") == "DeviceNotRegistered"
    )
