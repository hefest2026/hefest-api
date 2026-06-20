"""E2E tests against the live staging environment (hefest.adviz.bg).

Run with:
    HEFEST_STAGING_URL=https://hefest.adviz.bg \
    uv run pytest tests/test_e2e_staging.py -v

Skipped automatically when HEFEST_STAGING_URL is not set so CI doesn't
hit staging by accident.

After each run the rate-limit Redis keys for the test client IP are flushed
via the /internal/flush-ratelimit endpoint so the suite can be re-run
immediately. That endpoint is only wired in dev/staging (ENV != production).
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

BASE = os.environ.get("HEFEST_STAGING_URL", "").rstrip("/")
HEADERS = {
    "User-Agent": "hefest-e2e/1.0",
    "Accept": "application/json",
}

pytestmark = pytest.mark.skipif(
    not BASE,
    reason="HEFEST_STAGING_URL not set — skipping staging E2E tests",
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(
        base_url=BASE, headers=HEADERS, timeout=15, follow_redirects=False
    ) as c:
        yield c


@pytest.fixture(scope="session")
def verified_user(client: httpx.Client) -> dict[str, str]:
    """Register + verify a fresh account; return tokens and credentials."""
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    password = "StrongPass123!"

    reg = client.post(
        "/register",
        json={"email": email, "password": password, "full_name": "E2E User"},
    )
    assert reg.status_code == 201, f"register failed: {reg.text}"
    body = reg.json()
    if "verify_token" not in body:
        pytest.skip("staging not in dev mode — verify_token not exposed")

    verify = client.post("/auth/verify-email", json={"token": body["verify_token"]})
    assert verify.status_code == 200, f"verify-email failed: {verify.text}"
    vbody = verify.json()

    return {
        "email": email,
        "password": password,
        "access_token": vbody["access_token"],
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
        r = client.post(
            "/register",
            json={
                "email": f"e2e-reg-{uuid.uuid4().hex[:8]}@example.com",
                "password": "StrongPass123!",
                "full_name": "New User",
            },
        )
        assert r.status_code == 201
        assert "message" in r.json()

    def test_register_dev_returns_verify_token(self, client: httpx.Client) -> None:
        r = client.post(
            "/register",
            json={
                "email": f"e2e-tok-{uuid.uuid4().hex[:8]}@example.com",
                "password": "StrongPass123!",
                "full_name": "Token User",
            },
        )
        assert r.status_code == 201
        assert "verify_token" in r.json()

    def test_register_duplicate_email_409(
        self, verified_user: dict[str, str], client: httpx.Client
    ) -> None:
        r = client.post(
            "/register",
            json={
                "email": verified_user["email"],
                "password": "StrongPass123!",
                "full_name": "Dupe",
            },
        )
        assert r.status_code == 409
        assert r.headers.get("X-Error-Code") == "email_exists"

    def test_register_short_password_422(self, client: httpx.Client) -> None:
        r = client.post(
            "/register",
            json={
                "email": f"e2e-short-{uuid.uuid4().hex[:8]}@example.com",
                "password": "short",
                "full_name": "Short Pass",
            },
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

    def test_verify_token_expires_in_900(self, client: httpx.Client) -> None:
        # Register a fresh user to get a verify_token
        reg = client.post(
            "/register",
            json={
                "email": f"e2e-exp-{uuid.uuid4().hex[:8]}@example.com",
                "password": "StrongPass123!",
                "full_name": "Expires Check",
            },
        )
        assert reg.status_code == 201
        body = reg.json()
        if "verify_token" not in body:
            pytest.skip("verify_token not in dev response")
        verify = client.post("/auth/verify-email", json={"token": body["verify_token"]})
        assert verify.status_code == 200
        assert verify.json()["expires_in"] == 900  # 15 min


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_login_verified_200(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        r = client.post(
            "/login",
            json={
                "email": verified_user["email"],
                "password": verified_user["password"],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["expires_in"] == 900

    def test_login_sets_refresh_cookie(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        r = client.post(
            "/login",
            json={
                "email": verified_user["email"],
                "password": verified_user["password"],
            },
        )
        assert r.status_code == 200
        assert "hefest_refresh" in r.cookies

    def test_login_unverified_403(self, client: httpx.Client) -> None:
        email = f"e2e-unv-{uuid.uuid4().hex[:8]}@example.com"
        reg2 = client.post(
            "/register",
            json={"email": email, "password": "StrongPass123!", "full_name": "Unv2"},
        )
        if reg2.status_code == 201:
            r = client.post(
                "/login", json={"email": email, "password": "StrongPass123!"}
            )
            assert r.status_code == 403
            assert r.headers.get("X-Error-Code") == "email_not_verified"

    def test_login_wrong_password_401(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        r = client.post(
            "/login",
            json={"email": verified_user["email"], "password": "wrongpassword"},
        )
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "invalid_credentials"

    def test_login_nonexistent_user_401(self, client: httpx.Client) -> None:
        r = client.post(
            "/login", json={"email": "nobody@example.com", "password": "whatever"}
        )
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "invalid_credentials"


# ---------------------------------------------------------------------------
# Refresh token
# ---------------------------------------------------------------------------


class TestRefresh:
    def _fresh_cookie(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> httpx.Cookies:
        r = client.post(
            "/login",
            json={
                "email": verified_user["email"],
                "password": verified_user["password"],
            },
        )
        assert r.status_code == 200
        return r.cookies

    def test_refresh_rotates_token(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        cookies = self._fresh_cookie(client, verified_user)
        old_refresh = cookies["hefest_refresh"]
        r = client.post("/auth/refresh", cookies=cookies)
        assert r.status_code == 200
        assert "access_token" in r.json()
        assert r.cookies["hefest_refresh"] != old_refresh

    def test_refresh_replay_401_reuse_detected(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        cookies = self._fresh_cookie(client, verified_user)
        stale = httpx.Cookies(dict(cookies))
        client.post("/auth/refresh", cookies=stale)  # consumes it
        r = client.post("/auth/refresh", cookies=stale)  # replay
        assert r.status_code == 401
        assert r.headers.get("X-Error-Code") == "token_reuse_detected"

    def test_refresh_fake_token_401(self, client: httpx.Client) -> None:
        r = client.post(
            "/auth/refresh",
            cookies=httpx.Cookies({"hefest_refresh": "fake-token-value"}),
        )
        assert r.status_code == 401

    def test_refresh_no_token_401(self, client: httpx.Client) -> None:
        r = client.post("/auth/refresh")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_204(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        login = client.post(
            "/login",
            json={
                "email": verified_user["email"],
                "password": verified_user["password"],
            },
        )
        assert login.status_code == 200
        r = client.post("/auth/logout", cookies=login.cookies)
        assert r.status_code == 204

    def test_logout_revokes_refresh(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        login = client.post(
            "/login",
            json={
                "email": verified_user["email"],
                "password": verified_user["password"],
            },
        )
        assert login.status_code == 200
        cookies = login.cookies
        client.post("/auth/logout", cookies=cookies)
        r = client.post("/auth/refresh", cookies=cookies)
        assert r.status_code == 401

    def test_logout_all_204(
        self, client: httpx.Client, verified_user: dict[str, str]
    ) -> None:
        login = client.post(
            "/login",
            json={
                "email": verified_user["email"],
                "password": verified_user["password"],
            },
        )
        assert login.status_code == 200
        r = client.post(
            "/auth/logout-all",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            cookies=login.cookies,
        )
        assert r.status_code == 204

    def test_logout_all_no_auth_401(self, client: httpx.Client) -> None:
        r = client.post("/auth/logout-all")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting (HEF-13) — runs LAST: intentionally exhausts per-IP windows
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def flush_ratelimit_after_module(client: httpx.Client) -> None:  # type: ignore[return]
    """Flush this IP's rate-limit keys from staging Redis after the module runs."""
    yield
    # DELETE /internal/flush-ratelimit is wired only when HEFEST_ENV != production
    r = client.delete("/internal/flush-ratelimit")
    if r.status_code not in (200, 204, 404):
        # 404 means endpoint not wired (e.g. production guard); non-fatal
        print(f"\n[e2e] flush-ratelimit returned {r.status_code} — keys may persist")


class TestZRateLimiting:
    def test_login_rate_limit_triggers_429(self, client: httpx.Client) -> None:
        got_429 = False
        for _ in range(15):
            r = client.post("/login", json={"email": "rl@example.com", "password": "x"})
            if r.status_code == 429:
                got_429 = True
                assert "Retry-After" in r.headers
                break
        assert got_429, "expected 429 after repeated login attempts"

    def test_register_rate_limit_triggers_429(self, client: httpx.Client) -> None:
        got_429 = False
        for _ in range(8):
            r = client.post(
                "/register",
                json={
                    "email": f"rl-{uuid.uuid4().hex}@example.com",
                    "password": "StrongPass123!",
                    "full_name": "RL",
                },
            )
            if r.status_code == 429:
                got_429 = True
                assert "Retry-After" in r.headers
                break
        assert got_429, "expected 429 after repeated register attempts"
