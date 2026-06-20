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
import uuid
from collections.abc import Generator

import httpx
import pytest

BASE = os.environ.get("HEFEST_STAGING_URL", "").rstrip("/")
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

    return {
        "email": email,
        "password": password,
        "access_token": verify.json()["access_token"],
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
    def test_register_new_user_201(self, client: httpx.Client) -> None:
        r = _register(client, f"e2e-reg-{uuid.uuid4().hex[:8]}@example.com")
        assert r.status_code == 201
        assert "message" in r.json()

    def test_register_dev_exposes_verify_token(self, client: httpx.Client) -> None:
        r = _register(client, f"e2e-tok-{uuid.uuid4().hex[:8]}@example.com")
        assert r.status_code == 201
        assert "verify_token" in r.json()

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

    def test_verify_expires_in_is_900(self, client: httpx.Client) -> None:
        reg = _register(client, f"e2e-exp-{uuid.uuid4().hex[:8]}@example.com")
        if reg.status_code == 429:
            pytest.skip("rate-limited — skipping expires_in assertion")
        assert reg.status_code == 201
        body = reg.json()
        if "verify_token" not in body:
            pytest.skip("verify_token not in dev response")
        verify = client.post("/auth/verify-email", json={"token": body["verify_token"]})
        assert verify.status_code == 200
        assert verify.json()["expires_in"] == 900


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

    def test_login_unverified_403(self, client: httpx.Client) -> None:
        email = f"e2e-unv-{uuid.uuid4().hex[:8]}@example.com"
        reg = _register(client, email)
        if reg.status_code != 201:
            pytest.skip("register rate-limited; skipping unverified-login test")
        r = _login(client, email, "StrongPass123!")
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


class TestRefresh:
    def _fresh_login(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> str:
        """Log in and return the refresh token (also stored in client.cookies)."""
        r = _login(client, verified_user["email"], verified_user["password"])
        assert r.status_code == 200
        return str(r.cookies["hefest_refresh"])

    def test_refresh_rotates_token(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        old = self._fresh_login(client, verified_user)
        r = client.post("/auth/refresh")  # uses client's cookie jar
        assert r.status_code == 200
        assert "access_token" in r.json()
        assert client.cookies["hefest_refresh"] != old

    def test_refresh_replay_401_reuse_detected(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        stale = self._fresh_login(client, verified_user)
        client.post("/auth/refresh")  # consume — rotates token in jar
        client.cookies.set("hefest_refresh", stale)  # replay with stale token
        r = client.post("/auth/refresh")
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "token_reuse_detected"

    def test_refresh_fake_token_401(self, client: httpx.Client) -> None:
        client.cookies.set("hefest_refresh", "fake-token")
        assert client.post("/auth/refresh").status_code == 401

    def test_refresh_no_token_401(self, client: httpx.Client) -> None:
        client.cookies.delete("hefest_refresh")
        assert client.post("/auth/refresh").status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
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

    def test_logout_all_204(
        self,
        client: httpx.Client,
        verified_user: dict[str, str],
    ) -> None:
        login = self._do_login(client, verified_user)
        r = client.post(
            "/auth/logout-all",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            cookies=login.cookies,
        )
        assert r.status_code == 204

    def test_logout_all_no_auth_401(self, client: httpx.Client) -> None:
        assert client.post("/auth/logout-all").status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting — runs LAST: intentionally exhausts per-IP windows.
# The autouse fixture below flushes those keys after this module finishes.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def flush_ratelimit_after_module(
    client: httpx.Client,
) -> Generator[None, None, None]:
    """Flush this IP's rate-limit Redis keys after the module completes."""
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
