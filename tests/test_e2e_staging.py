"""E2E tests against a live environment.

Run against staging:
    HEFEST_STAGING_URL=https://hefest.adviz.bg \\
        uv run pytest tests/test_e2e_staging.py -v

Run against local compose:
    HEFEST_STAGING_URL=http://localhost:8000 \\
        uv run pytest tests/test_e2e_staging.py -v

Skipped automatically when HEFEST_STAGING_URL is not set so normal CI
(unit/integration) never hits a live environment.

After the rate-limit class runs, an autouse fixture calls
DELETE /internal/flush-ratelimit to clear this IP's Redis keys so the
suite can be re-run immediately without waiting for the window to expire.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Generator

import httpx
import pytest

BASE = os.environ.get("HEFEST_STAGING_URL", "").rstrip("/")
# Mailpit HTTP API base; set only where a mail catcher is reachable (local
# compose / CI e2e job). Absent against real staging (Resend), so the
# email-delivery test below skips there.
MAILPIT = os.environ.get("HEFEST_MAILPIT_URL", "").rstrip("/")
HEADERS = {"User-Agent": "hefest-e2e/1.0", "Accept": "application/json"}

pytestmark = pytest.mark.skipif(
    not BASE,
    reason="HEFEST_STAGING_URL not set — skipping E2E tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: httpx.Client, email: str, password: str) -> httpx.Response:
    return client.post("/login", json={"email": email, "password": password})


def _register(
    client: httpx.Client,
    email: str,
    password: str = "StrongPass123!",
    full_name: str = "E2E User",
) -> httpx.Response:
    return client.post(
        "/register",
        json={"email": email, "password": password, "full_name": full_name},
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client() -> Generator[httpx.Client, None, None]:
    with httpx.Client(
        base_url=BASE,
        headers=HEADERS,
        timeout=15,
        follow_redirects=False,
    ) as c:
        yield c


@pytest.fixture(scope="session")
def unverified_user(client: httpx.Client) -> dict[str, str]:
    """Register once without verifying — stays unverified for the full session."""
    email = f"e2e-unv-{uuid.uuid4().hex[:8]}@example.com"
    password = "StrongPass123!"
    reg = _register(client, email, password)
    assert reg.status_code == 201, f"unverified register failed: {reg.text}"
    return {"email": email, "password": password}


@pytest.fixture(scope="session")
def verified_user(client: httpx.Client) -> dict[str, str]:
    """Register + verify a fresh account; return tokens and credentials."""
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    password = "StrongPass123!"

    reg = _register(client, email, password)
    assert reg.status_code == 201, f"register failed: {reg.text}"
    body = reg.json()
    if "verify_token" not in body:
        pytest.skip("server not in dev mode — verify_token not exposed")

    verify = client.post("/auth/verify-email", json={"token": body["verify_token"]})
    assert verify.status_code == 200, f"verify-email failed: {verify.text}"

    vbody = verify.json()
    return {
        "email": email,
        "password": password,
        "access_token": vbody["access_token"],
        "expires_in": str(vbody.get("expires_in", "")),
        "refresh_cookie": dict(verify.cookies).get("hefest_refresh", ""),
    }


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


class TestOperational:
    def test_health(self, client: httpx.Client) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_ready_all_deps_up(self, client: httpx.Client) -> None:
        r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["postgres"] == "ok"
        assert body["redis"] == "ok"


# ---------------------------------------------------------------------------
# Providers / SSO discovery
# ---------------------------------------------------------------------------


class TestProviders:
    def test_password_available(self, client: httpx.Client) -> None:
        r = client.get("/auth/providers")
        assert r.status_code == 200
        assert r.json()["password"]["available"] is True

    def test_google_and_microsoft_listed(self, client: httpx.Client) -> None:
        names = [p["name"] for p in client.get("/auth/providers").json()["providers"]]
        assert "google" in names
        assert "microsoft" in names

    def test_disabled_sso_google_login_404(self, client: httpx.Client) -> None:
        r = client.get("/auth/google/login")
        assert r.status_code == 404
        assert r.headers.get("X-Error-Code") == "sso_provider_disabled"

    def test_disabled_sso_microsoft_login_404(self, client: httpx.Client) -> None:
        r = client.get("/auth/microsoft/login")
        assert r.status_code == 404
        assert r.headers.get("X-Error-Code") == "sso_provider_disabled"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_201_returns_message_and_verify_token(
        self, client: httpx.Client
    ) -> None:
        r = _register(client, f"e2e-reg-{uuid.uuid4().hex[:8]}@example.com")
        assert r.status_code == 201
        body = r.json()
        assert "message" in body
        assert "verify_token" in body

    def test_register_duplicate_email_409(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        r = _register(client, verified_user["email"])
        assert r.status_code == 409
        assert r.headers.get("X-Error-Code") == "email_exists"

    def test_register_short_password_422(self, client: httpx.Client) -> None:
        r = _register(
            client,
            f"e2e-short-{uuid.uuid4().hex[:8]}@example.com",
            password="short",
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Verify-email
# ---------------------------------------------------------------------------


class TestVerifyEmail:
    def test_verify_bad_token_400(self, client: httpx.Client) -> None:
        r = client.post("/auth/verify-email", json={"token": "not-a-real-token"})
        assert r.status_code == 400

    def test_verify_issues_access_token(self, verified_user: dict[str, str]) -> None:
        assert verified_user["access_token"]

    def test_verify_sets_refresh_cookie(self, verified_user: dict[str, str]) -> None:
        assert verified_user["refresh_cookie"]

    def test_verify_expires_in_is_900(self, verified_user: dict[str, str]) -> None:
        assert verified_user["expires_in"] == "900"


# ---------------------------------------------------------------------------
# Email delivery — proves the outbox worker actually sends the verification
# email. Requires a reachable mail catcher (mailpit); skipped otherwise.
# ---------------------------------------------------------------------------


def _wait_for_verification_email(to: str, timeout: float = 30.0) -> str:
    """Poll mailpit until a message addressed to ``to`` arrives; return its body.

    Args:
        to: Recipient address to search for.
        timeout: Max seconds to wait for the asynchronously-delivered email.

    Returns:
        The plain-text body of the first matching message.

    Raises:
        AssertionError: If no matching email arrives before ``timeout``.
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=MAILPIT, timeout=10) as mc:
        while time.monotonic() < deadline:
            search = mc.get("/api/v1/search", params={"query": f"to:{to}"})
            if search.status_code == 200:
                messages = search.json().get("messages", [])
                if messages:
                    detail = mc.get(f"/api/v1/message/{messages[0]['ID']}")
                    detail.raise_for_status()
                    return detail.json().get("Text", "")
            time.sleep(1.0)
    raise AssertionError(f"no verification email for {to} within {timeout:.0f}s")


@pytest.mark.skipif(
    not MAILPIT,
    reason="HEFEST_MAILPIT_URL not set — no mail catcher to assert delivery",
)
class TestEmailDelivery:
    def test_verification_email_delivered_and_link_verifies(
        self, client: httpx.Client
    ) -> None:
        """register -> worker delivers the email -> its link verifies the account."""
        # Defensive flush so this extra registration never trips the 5/hour
        # register limit depending on suite ordering.
        client.delete("/internal/flush-ratelimit")

        email = f"e2e-mail-{uuid.uuid4().hex[:8]}@example.com"
        reg = _register(client, email)
        assert reg.status_code == 201, f"register failed: {reg.text}"

        body = _wait_for_verification_email(email)
        assert "verify" in body.lower()

        match = re.search(r"token=([A-Za-z0-9._-]+)", body)
        assert match, f"no verify token in delivered email body: {body!r}"
        emailed_token = match.group(1)

        # The token the user actually RECEIVED (not the dev-mode response token)
        # must verify the account end-to-end.
        verify = client.post("/auth/verify-email", json={"token": emailed_token})
        assert verify.status_code == 200, (
            f"verify with emailed token failed: {verify.text}"
        )

        login = _login(client, email, "StrongPass123!")
        assert login.status_code == 200, (
            f"login after email verify failed: {login.text}"
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_login_verified_200(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        r = _login(client, verified_user["email"], verified_user["password"])
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["expires_in"] == 900

    def test_login_sets_refresh_cookie(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        r = _login(client, verified_user["email"], verified_user["password"])
        assert r.status_code == 200
        assert "hefest_refresh" in r.cookies

    def test_login_unverified_403(
        self, client: httpx.Client, unverified_user: dict[str, str]
    ) -> None:
        r = _login(client, unverified_user["email"], unverified_user["password"])
        assert r.status_code == 403
        assert r.headers.get("X-Error-Code") == "email_not_verified"

    def test_login_wrong_password_401(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        r = _login(client, verified_user["email"], "wrongpassword")
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "invalid_credentials"

    def test_login_nonexistent_user_401(self, client: httpx.Client) -> None:
        r = _login(client, "nobody@example.com", "whatever")
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "invalid_credentials"


# ---------------------------------------------------------------------------
# Refresh token
# ---------------------------------------------------------------------------


def _refresh_client(token: str) -> httpx.Client:
    """Fresh one-shot client carrying exactly one refresh token.

    Using isolated clients (not the session-scoped one) prevents the shared
    cookie jar from leaking tokens across refresh tests.
    """
    return httpx.Client(
        base_url=BASE,
        headers=HEADERS,
        timeout=15,
        follow_redirects=False,
        cookies={"hefest_refresh": token},
    )


class TestRefresh:
    def _get_token(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> str:
        """Log in and return the fresh refresh token from the response."""
        r = _login(client, verified_user["email"], verified_user["password"])
        assert r.status_code == 200
        return str(r.cookies["hefest_refresh"])

    def test_refresh_rotates_token(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        token = self._get_token(client, verified_user)
        with _refresh_client(token) as c:
            r = c.post("/auth/refresh")
        assert r.status_code == 200
        assert "access_token" in r.json()
        assert r.cookies["hefest_refresh"] != token

    def test_refresh_replay_401_reuse_detected(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        stale = self._get_token(client, verified_user)
        with _refresh_client(stale) as c:
            assert c.post("/auth/refresh").status_code == 200  # consume
        with _refresh_client(stale) as c:
            r = c.post("/auth/refresh")  # replay
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "token_reuse_detected"

    def test_refresh_fake_token_401(self) -> None:
        with _refresh_client("fake-token") as c:
            assert c.post("/auth/refresh").status_code == 401

    def test_refresh_no_token_401(self, client: httpx.Client) -> None:
        with httpx.Client(base_url=BASE, headers=HEADERS, timeout=15) as c:
            assert c.post("/auth/refresh").status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    @pytest.fixture(scope="class", autouse=True)
    def _flush_login_ratelimit(self, client: httpx.Client) -> None:
        """Reset per-IP rate-limit keys before this login-heavy class.

        The preceding classes consume most of the 10/60s login window; the
        logout-all coverage below needs several fresh logins of its own.
        """
        client.delete("/internal/flush-ratelimit")

    def _do_login(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> httpx.Response:
        r = _login(client, verified_user["email"], verified_user["password"])
        assert r.status_code == 200
        return r

    def test_logout_204(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        login = self._do_login(client, verified_user)
        assert client.post("/auth/logout", cookies=login.cookies).status_code == 204

    def test_logout_revokes_refresh(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        login = self._do_login(client, verified_user)
        cookies = login.cookies
        client.post("/auth/logout", cookies=cookies)
        assert client.post("/auth/refresh", cookies=cookies).status_code == 401

    def test_logout_all_bearer_204(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        login = self._do_login(client, verified_user)
        r = client.post(
            "/auth/logout-all",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert r.status_code == 204

    def test_logout_all_cookie_only_204(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        """logout-all works from a browser session holding only the refresh cookie."""
        raw = str(self._do_login(client, verified_user).cookies["hefest_refresh"])
        r = client.post("/auth/logout-all", cookies={"hefest_refresh": raw})
        assert r.status_code == 204

    def test_logout_all_cookie_revokes_all_sessions(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        """logout-all via cookie revokes refresh tokens issued to other sessions."""
        first = str(self._do_login(client, verified_user).cookies["hefest_refresh"])
        second = str(self._do_login(client, verified_user).cookies["hefest_refresh"])
        assert (
            client.post(
                "/auth/logout-all", cookies={"hefest_refresh": second}
            ).status_code
            == 204
        )
        # the first session's refresh token must now be rejected too
        with _refresh_client(first) as c:
            assert c.post("/auth/refresh").status_code == 401

    def test_logout_all_no_auth_401(self, client: httpx.Client) -> None:
        assert client.post("/auth/logout-all").status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting — runs LAST: intentionally exhausts per-IP windows.
# The autouse fixture below flushes those keys after this module finishes.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def flush_ratelimit_around_module(
    client: httpx.Client,
) -> Generator[None, None, None]:
    """Flush rate-limit Redis keys before AND after the Z-rate-limit class.

    A pre-flush ensures this class starts from a clean slate regardless of
    how many /register or /login calls the preceding tests consumed. The
    post-flush lets the suite be re-run immediately.
    """
    client.delete("/internal/flush-ratelimit")
    yield
    r = client.delete("/internal/flush-ratelimit")
    if r.status_code not in (200, 204, 404):
        print(f"\n[e2e] flush-ratelimit → {r.status_code}; keys may persist")


class TestZRateLimiting:
    def test_login_rate_limit_triggers_429(self, client: httpx.Client) -> None:
        got_429 = False
        for _ in range(15):
            r = _login(client, "rl@example.com", "x")
            if r.status_code == 429:
                got_429 = True
                assert "Retry-After" in r.headers
                break
        assert got_429, "expected 429 after repeated login attempts"

    def test_register_rate_limit_triggers_429(self, client: httpx.Client) -> None:
        got_429 = False
        for _ in range(8):
            r = _register(client, f"rl-{uuid.uuid4().hex}@example.com")
            if r.status_code == 429:
                got_429 = True
                assert "Retry-After" in r.headers
                break
        assert got_429, "expected 429 after repeated register attempts"
