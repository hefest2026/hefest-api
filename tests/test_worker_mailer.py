"""Unit tests for hefest/worker/mailer.py — kept-alive SMTP pool + error classification.

Settings stub
-------------
We use a plain ``dataclass`` rather than constructing the real pydantic-settings
``Settings`` (which would attempt to read env vars / the .env file and fail in
CI without a full environment).  The stub exposes exactly the ``smtp_*`` and
``worker_send_concurrency`` attributes that ``Mailer`` reads.

Patching strategy
-----------------
``monkeypatch.setattr(mailer, "SMTP", FakeSMTPFactory)`` replaces the ``SMTP``
symbol imported into the ``mailer`` module *before* ``Mailer()`` is constructed,
so ``__init__`` builds pool entries from the fake class.  A factory closure
captures the shared ``FakeSMTP`` instance so tests can inspect calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import pytest
from aiosmtplib import (
    SMTPConnectError,
    SMTPDataError,
    SMTPRecipientRefused,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
    SMTPTimeoutError,
)

from hefest.worker import mailer as mailer_module
from hefest.worker.mailer import Mailer, PermanentSendError, TransientSendError
from hefest.worker.templates import EmailContent

# ---------------------------------------------------------------------------
# Settings stub
# ---------------------------------------------------------------------------


@dataclass
class FakeSettings:
    """Minimal settings stub; exposes only what Mailer reads."""

    smtp_host: str = "smtp.test"
    smtp_port: int = 587
    smtp_from: str = "noreply@test.local"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    smtp_timeout: int = 5
    worker_send_concurrency: int = 1


# ---------------------------------------------------------------------------
# Fake SMTP client
# ---------------------------------------------------------------------------


class FakeSMTP:
    """Controllable stand-in for ``aiosmtplib.SMTP``.

    Side-effect callables (``_connect_side_effect``, ``_quit_side_effect``)
    allow tests to inject errors at connect/quit time without patching bound
    methods (which causes type-checker errors).

    Attributes:
        is_connected: Mutable; starts ``False`` (simulates unconnected state).
        connect_count: Number of times ``connect()`` was called.
        login_count: Number of times ``login()`` was called.
        sent_messages: All ``EmailMessage`` objects passed to ``send_message``.
        send_timeouts: Per-call ``timeout`` kwarg values from ``send_message``.
        quit_count: Number of times ``quit()`` was called.
        _raise_on_send: If set, ``send_message`` raises this exception once.
        _connect_side_effect: Optional coroutine function to call inside
            ``connect()``; raised exceptions propagate to the caller.
        _quit_side_effect: Optional coroutine function to call inside
            ``quit()``; raised exceptions propagate to the caller.
    """

    def __init__(self, **kwargs: object) -> None:
        self.is_connected: bool = False
        self.connect_count: int = 0
        self.login_count: int = 0
        self.sent_messages: list[EmailMessage] = []
        self.send_timeouts: list[float | None] = []
        self.quit_count: int = 0
        self._raise_on_send: BaseException | None = None
        self._connect_side_effect: Callable[[], Awaitable[None]] | None = None
        self._quit_side_effect: Callable[[], Awaitable[None]] | None = None

    async def connect(self) -> None:
        """Mark the fake as connected (or call side-effect if configured)."""
        if self._connect_side_effect is not None:
            await self._connect_side_effect()
        self.is_connected = True
        self.connect_count += 1

    async def login(self, username: str, password: str) -> None:
        """Record login call."""
        self.login_count += 1

    async def send_message(
        self, msg: EmailMessage, *, timeout: float | None = None
    ) -> tuple[dict[str, Any], str]:
        """Capture the message or raise a pre-configured exception.

        When ``_raise_on_send`` is set, the exception is consumed (reset to
        ``None``) and raised.  Connection-level errors also flip
        ``is_connected`` to ``False`` to mirror real aiosmtplib behaviour.

        Args:
            msg: The email message to (fake) send.
            timeout: Per-send timeout forwarded by the mailer.

        Returns:
            An empty per-recipient response dict and an ``"OK"`` message.

        Raises:
            Whatever was placed in ``_raise_on_send``.
        """
        if self._raise_on_send is not None:
            exc = self._raise_on_send
            self._raise_on_send = None
            if isinstance(exc, (SMTPServerDisconnected, SMTPConnectError, OSError)):
                self.is_connected = False
            raise exc
        self.sent_messages.append(msg)
        self.send_timeouts.append(timeout)
        return {}, "OK"

    async def quit(self) -> None:
        """Record quit call (or call side-effect if configured)."""
        if self._quit_side_effect is not None:
            await self._quit_side_effect()
        self.quit_count += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTENT = EmailContent(subject="Test subject", body="Test body.")


def _make_mailer(
    monkeypatch: pytest.MonkeyPatch, settings: FakeSettings
) -> tuple[Mailer, FakeSMTP]:
    """Patch ``mailer.SMTP`` and construct a ``Mailer``, returning both.

    The single pool slot is filled with the ``FakeSMTP`` instance returned
    alongside the ``Mailer``.

    Args:
        monkeypatch: pytest fixture for patching.
        settings: Stub settings to inject.

    Returns:
        A ``(Mailer, FakeSMTP)`` tuple.
    """
    fake = FakeSMTP()

    def _factory(**kwargs: Any) -> FakeSMTP:
        return fake

    monkeypatch.setattr(mailer_module, "SMTP", _factory)
    m = Mailer(settings)
    return m, fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_success_message_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """send() builds a message with the correct From/To/Subject/body and
    calls send_message with timeout=smtp_timeout."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)

    await m.send(_CONTENT, "recipient@example.com")

    assert len(fake.sent_messages) == 1
    msg = fake.sent_messages[0]
    assert msg["From"] == settings.smtp_from
    assert msg["To"] == "recipient@example.com"
    assert msg["Subject"] == _CONTENT.subject
    assert _CONTENT.body in msg.get_content()
    assert fake.send_timeouts[0] == settings.smtp_timeout


async def test_lazy_connect_without_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First send() connects; login is NOT called when smtp_username is empty."""
    settings = FakeSettings(smtp_username="")
    m, fake = _make_mailer(monkeypatch, settings)

    assert fake.connect_count == 0
    await m.send(_CONTENT, "a@b.com")

    assert fake.connect_count == 1
    assert fake.login_count == 0


async def test_lazy_connect_with_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """First send() connects AND logs in when smtp_username is non-empty."""
    settings = FakeSettings(smtp_username="user", smtp_password="secret")
    m, fake = _make_mailer(monkeypatch, settings)

    await m.send(_CONTENT, "a@b.com")

    assert fake.connect_count == 1
    assert fake.login_count == 1


async def test_no_reconnect_when_already_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second send() does NOT connect again when is_connected is True."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)

    await m.send(_CONTENT, "a@b.com")
    await m.send(_CONTENT, "b@b.com")

    assert fake.connect_count == 1
    assert len(fake.sent_messages) == 2


async def test_5xx_raises_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 550 response raises PermanentSendError."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    fake._raise_on_send = SMTPResponseException(550, "User not found")

    with pytest.raises(PermanentSendError, match="550"):
        await m.send(_CONTENT, "bad@example.com")


async def test_5xx_data_error_raises_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTPDataError (subclass of SMTPResponseException) 5xx raises Permanent."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    fake._raise_on_send = SMTPDataError(554, "Transaction failed")

    with pytest.raises(PermanentSendError, match="554"):
        await m.send(_CONTENT, "x@example.com")


async def test_4xx_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 421 response raises TransientSendError."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    fake._raise_on_send = SMTPResponseException(421, "Service temporarily unavailable")

    with pytest.raises(TransientSendError, match="421"):
        await m.send(_CONTENT, "x@example.com")


async def test_timeout_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMTPTimeoutError (which IS asyncio.TimeoutError) raises TransientSendError."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    fake._raise_on_send = SMTPTimeoutError("timed out")

    with pytest.raises(TransientSendError, match="timeout"):
        await m.send(_CONTENT, "x@example.com")


async def test_asyncio_timeout_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain TimeoutError also maps to TransientSendError."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    fake._raise_on_send = TimeoutError()

    with pytest.raises(TransientSendError):
        await m.send(_CONTENT, "x@example.com")


async def test_disconnect_raises_transient_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTPServerDisconnected → TransientSendError; next send() reconnects."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)

    # First send: connects OK, then send_message raises disconnect.
    fake._raise_on_send = SMTPServerDisconnected("connection lost")
    with pytest.raises(TransientSendError):
        await m.send(_CONTENT, "x@example.com")

    # is_connected flipped False by FakeSMTP when connection error raised.
    assert not fake.is_connected

    # Second send: must reconnect because is_connected is False.
    await m.send(_CONTENT, "x@example.com")
    assert fake.connect_count == 2
    assert len(fake.sent_messages) == 1  # first failed, second succeeded


async def test_connect_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTPConnectError during _ensure_connected raises TransientSendError."""
    settings = FakeSettings()
    fake = FakeSMTP()
    connect_calls: list[int] = [0]

    async def _bad_connect() -> None:
        connect_calls[0] += 1
        raise SMTPConnectError("refused")

    fake._connect_side_effect = _bad_connect

    def _factory(**kwargs: Any) -> FakeSMTP:
        return fake

    monkeypatch.setattr(mailer_module, "SMTP", _factory)
    m = Mailer(settings)

    with pytest.raises(TransientSendError, match="connection error"):
        await m.send(_CONTENT, "x@example.com")

    assert connect_calls[0] == 1


async def test_recipients_refused_5xx_raises_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTPRecipientsRefused with 5xx codes raises PermanentSendError."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    refused = [SMTPRecipientRefused(550, "No such user", "bad@example.com")]
    fake._raise_on_send = SMTPRecipientsRefused(refused)

    with pytest.raises(PermanentSendError):
        await m.send(_CONTENT, "bad@example.com")


async def test_recipients_refused_4xx_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTPRecipientsRefused with 4xx codes raises TransientSendError."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    refused = [SMTPRecipientRefused(450, "Mailbox unavailable", "x@example.com")]
    fake._raise_on_send = SMTPRecipientsRefused(refused)

    with pytest.raises(TransientSendError):
        await m.send(_CONTENT, "x@example.com")


async def test_pool_always_returns_client_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool client is returned even when send() raises an error."""
    settings = FakeSettings()
    m, fake = _make_mailer(monkeypatch, settings)
    fake._raise_on_send = SMTPResponseException(550, "error")

    with pytest.raises(PermanentSendError):
        await m.send(_CONTENT, "x@example.com")

    # Pool must not be empty; a subsequent send must succeed immediately.
    assert not m._pool.empty()
    await m.send(_CONTENT, "x@example.com")
    assert len(fake.sent_messages) == 1


async def test_aclose_quits_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """aclose() calls quit() on every pooled client."""
    settings = FakeSettings(worker_send_concurrency=2)
    fakes: list[FakeSMTP] = []

    def _factory(**kwargs: Any) -> FakeSMTP:
        f = FakeSMTP()
        fakes.append(f)
        return f

    monkeypatch.setattr(mailer_module, "SMTP", _factory)
    m = Mailer(settings)

    await m.aclose()

    assert len(fakes) == 2
    for f in fakes:
        assert f.quit_count == 1
    assert m._pool.empty()


async def test_aclose_suppresses_quit_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aclose() does not propagate exceptions from quit()."""
    settings = FakeSettings()
    fake = FakeSMTP()

    async def _bad_quit() -> None:
        raise OSError("socket gone")

    fake._quit_side_effect = _bad_quit

    def _factory(**kwargs: Any) -> FakeSMTP:
        return fake

    monkeypatch.setattr(mailer_module, "SMTP", _factory)
    m = Mailer(settings)

    # Must not raise.
    await m.aclose()
