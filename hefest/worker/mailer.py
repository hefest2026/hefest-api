"""Kept-alive SMTP connection pool with transient/permanent error classification.

Each ``Mailer`` owns a fixed-size pool of ``aiosmtplib.SMTP`` clients (one per
concurrency slot).  Connections are established lazily on first use and reused
across jobs so that every email delivery does NOT pay a fresh TCP/TLS handshake
— critical for Resend's connection-rate limits.

Error taxonomy
--------------
``TransientSendError`` — timeouts, disconnects, 4xx responses.  The consumer
retries with backoff.
``PermanentSendError``  — 5xx responses.  The consumer parks the job as
``failed``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from email.message import EmailMessage
from typing import Protocol

from aiosmtplib import (
    SMTP,
    SMTPConnectError,
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
)

from hefest.worker.errors import PermanentError, TransientError
from hefest.worker.templates import EmailContent

logger = logging.getLogger(__name__)


class _MailerConfig(Protocol):
    """Structural interface for the settings consumed by ``Mailer``.

    Using a ``Protocol`` keeps ``Mailer`` testable without importing the
    concrete ``Settings`` class, while still accepting the real ``Settings``
    instance (structural sub-typing).
    """

    smtp_host: str
    smtp_port: int
    smtp_from: str
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    smtp_timeout: int
    worker_send_concurrency: int


class TransientSendError(TransientError):
    """A transient SMTP failure; the consumer should retry with backoff."""


class PermanentSendError(PermanentError):
    """A permanent SMTP failure; retrying cannot help."""


def _classify_smtp_error(exc: Exception) -> TransientSendError | PermanentSendError:
    """Map a raw exception to the correct send-error subclass.

    Args:
        exc: The raw exception from aiosmtplib or the OS layer.

    Returns:
        A ``TransientSendError`` or ``PermanentSendError`` (not yet raised).
    """
    # asyncio.TimeoutError covers SMTPTimeoutError (which inherits from it).
    if isinstance(exc, asyncio.TimeoutError):
        return TransientSendError(f"SMTP timeout: {exc}")

    # SMTPRecipientsRefused does NOT inherit SMTPResponseException; its code
    # lives inside the nested SMTPRecipientRefused objects.
    if isinstance(exc, SMTPRecipientsRefused):
        codes = [r.code for r in exc.recipients]
        if codes and max(codes) >= 500:
            return PermanentSendError(f"SMTP recipients refused (5xx): {exc}")
        return TransientSendError(f"SMTP recipients refused (4xx/transient): {exc}")

    # SMTPResponseException (and all subclasses: SMTPDataError,
    # SMTPSenderRefused, SMTPRecipientRefused, SMTPHeloError,
    # SMTPAuthenticationError, SMTPConnectResponseError …).
    if isinstance(exc, SMTPResponseException):
        if exc.code >= 500:
            return PermanentSendError(f"SMTP {exc.code}: {exc.message}")
        return TransientSendError(f"SMTP {exc.code}: {exc.message}")

    # Connection-level failures — the socket is dead; next checkout reconnects.
    if isinstance(exc, (SMTPServerDisconnected, SMTPConnectError, OSError)):
        return TransientSendError(f"SMTP connection error: {exc}")

    # Residual SMTPException subclasses (e.g. SMTPNotSupported) — conservative:
    # retry rather than silently park a potentially-deliverable email.
    if isinstance(exc, SMTPException):
        return TransientSendError(f"SMTP error: {exc}")

    # Unexpected exception type — still raise as transient so the consumer can
    # retry to the attempt cap rather than losing the job silently.
    return TransientSendError(f"Unexpected SMTP error: {exc}")


class Mailer:
    """Kept-alive SMTP connection pool.

    Attributes:
        _settings: Injected ``Settings`` instance; never reads the global
            singleton so the class is fully testable.
        _pool: Bounded asyncio queue of pre-configured (but not yet connected)
            ``SMTP`` clients.
    """

    def __init__(self, settings: _MailerConfig) -> None:
        """Initialise the pool.  No I/O; connections are established lazily.

        Args:
            settings: Runtime config; reads ``smtp_*`` and
                ``worker_send_concurrency``.  Any object satisfying
                ``_MailerConfig`` is accepted (including the real
                ``hefest.config.Settings`` and test stubs).
        """
        self._settings = settings
        pool_size = settings.worker_send_concurrency
        self._pool: asyncio.Queue[SMTP] = asyncio.Queue(maxsize=pool_size)
        for _ in range(pool_size):
            client = SMTP(
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                timeout=settings.smtp_timeout,
                use_tls=settings.smtp_use_tls,
            )
            self._pool.put_nowait(client)

    async def send(self, content: EmailContent, to: str) -> None:
        """Check out a connection, send one email, return the connection.

        The per-send timeout is ``settings.smtp_timeout``.  On any failure the
        exception is classified and re-raised; the connection is always
        returned to the pool (``finally`` block).

        Args:
            content: Rendered email subject and plain-text body.
            to: Recipient email address.

        Raises:
            TransientSendError: Timeout, disconnect, or 4xx SMTP response.
            PermanentSendError: 5xx SMTP response.
        """
        client = await self._pool.get()
        try:
            await self._ensure_connected(client)
            msg = EmailMessage()
            msg["From"] = self._settings.smtp_from
            msg["To"] = to
            msg["Subject"] = content.subject
            msg.set_content(content.body)
            await client.send_message(msg, timeout=self._settings.smtp_timeout)
        except (TransientSendError, PermanentSendError):
            raise
        except Exception as exc:
            raise _classify_smtp_error(exc) from exc
        finally:
            self._pool.put_nowait(client)

    async def _ensure_connected(self, client: SMTP) -> None:
        """Connect and authenticate if the client is not currently connected.

        Args:
            client: A pooled ``SMTP`` instance (may be disconnected).

        Raises:
            Any aiosmtplib exception from ``connect()`` or ``login()``, which
            the caller (``send``) will classify.
        """
        if not client.is_connected:
            await client.connect()
            if self._settings.smtp_username:
                await client.login(
                    self._settings.smtp_username,
                    self._settings.smtp_password,
                )

    async def aclose(self) -> None:
        """Gracefully close every pooled client.

        Errors during quit are suppressed; the pool is fully drained.
        """
        while not self._pool.empty():
            client = self._pool.get_nowait()
            with suppress(Exception):
                await client.quit()
