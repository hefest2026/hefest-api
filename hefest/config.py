from __future__ import annotations

from typing import Final, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_OAUTH_PROVIDERS: Final = ("google", "microsoft")

WORKER_DB_POOL_HEADROOM: Final = 3
"""Connections the worker needs beyond its in-flight sends: the claim, the
heartbeat, and the reaper queries run concurrently with finalizers, so
``worker_db_pool_size`` must leave at least this much room above
``worker_send_concurrency``."""


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables (prefix HEFEST_)."""

    model_config = SettingsConfigDict(env_prefix="HEFEST_", env_file=".env")

    env: str = "dev"
    log_level: str = "DEBUG"

    db_url: str = "asyncpg://hefest:hefest@localhost:5432/hefest_db"
    redis_url: str = "redis://localhost:6379"

    # JWT / token fields
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_audience: str = "hefest-api"
    jwt_expire_minutes: int = 15
    refresh_token_expire_days: int = 14
    email_verify_expire_hours: int = 24

    # Cookie / CORS fields
    refresh_cookie_name: str = "hefest_refresh"
    refresh_cookie_secure: bool = True
    frontend_oauth_success_url: str = ""
    cors_origins: list[str] = []

    # OAuth fields
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = ""
    microsoft_redirect_uri: str = ""

    # notification delivery worker — Postgres-outbox driven email sender.
    #
    # The worker is push-driven via PostgreSQL LISTEN/NOTIFY (channel below): an
    # AFTER INSERT trigger on notification_jobs fires NOTIFY at COMMIT, waking the
    # worker in ~milliseconds instead of waiting for a poll tick. NOTIFY is
    # fire-and-forget (at-most-once) — a signal emitted while the worker is
    # disconnected is lost — so a long-interval fallback poll guarantees eventual
    # drain and catch-up after downtime. The outbox row itself is the durable
    # source of truth; NOTIFY only collapses latency. See worker/consumer.py.
    worker_notify_channel: str = "hefest_jobs"
    worker_fallback_poll_interval: float = 5.0
    """Safety-net poll cadence (seconds) for jobs whose NOTIFY was missed."""
    worker_claim_batch_size: int = 50
    """Max pending rows claimed per pass (FOR UPDATE SKIP LOCKED).

    Worst case ceil(50/10)*30s = 150s to fully drain one batch at
    worker_send_concurrency=10 and worker_backoff_base_seconds=30, which stays
    well under worker_reaper_idle_seconds (300) so claimed-but-stalled rows are
    still reaped correctly.
    """
    worker_reaper_idle_seconds: int = 300
    """Lease age after which a claimed-but-unfinished job is reclaimed."""
    worker_reap_batch_size: int = 1000
    """Max stale leases reclaimed per reap transaction. Bounds the reap so a
    large backlog after downtime is recovered in several small transactions —
    partitioned across concurrent workers via ``FOR UPDATE SKIP LOCKED`` — rather
    than one giant UPDATE that holds locks, spikes WAL, and stalls autovacuum."""
    worker_heartbeat_interval: int = 90
    """Lease renewal cadence; must stay <= 1/3 of worker_reaper_idle_seconds
    (300) so a live worker renews its lease at least twice before the reaper
    would otherwise consider it dead."""
    worker_max_attempts: int = 3
    """Max delivery attempts before a job is permanently failed."""
    worker_backoff_base_seconds: int = 30
    """Base for the retry backoff applied between delivery attempts."""
    worker_send_concurrency: int = 10
    """Max concurrent in-flight SMTP sends."""
    worker_db_pool_size: int = 13
    """asyncpg pool size for the worker's own Tortoise connection (see
    build_worker_tortoise_orm). Must be >= worker_send_concurrency (10) plus
    headroom for the claim, heartbeat, and reaper queries running
    concurrently with in-flight sends."""

    # SMTP (mailpit in dev compose, Resend in prod)
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "noreply@hefest.local"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    smtp_timeout: int = 30
    """Per-send SMTP timeout (seconds); must stay well under
    worker_heartbeat_interval (90), which itself is well under
    worker_reaper_idle_seconds (300), so a hung send is never mistaken for a
    healthy lease."""

    # trusted reverse proxies (ProxyHeadersMiddleware uses this list)
    # set to the nginx container IP or CIDR in production
    trusted_proxies: list[str] = ["127.0.0.1", "::1"]

    # event business rules
    event_location_lock_hours: int = 2
    """Hours before event start within which the location can no longer be changed."""

    # rate limiting
    rate_limit_login_count: int = 10
    rate_limit_login_window_seconds: int = 60
    rate_limit_register_count: int = 5
    rate_limit_register_window_seconds: int = 3600
    rate_limit_event_register_count: int = 30
    rate_limit_event_register_window_seconds: int = 60
    rate_limit_global_count: int = 200
    rate_limit_global_window_seconds: int = 60

    @property
    def enabled_oauth_providers(self) -> list[str]:
        """Return list of enabled OAuth providers based on configuration."""
        out: list[str] = []
        if (
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
        ):
            out.append("google")
        if all(
            (
                self.microsoft_client_id,
                self.microsoft_client_secret,
                self.microsoft_redirect_uri,
                self.microsoft_tenant,
            )
        ):
            out.append("microsoft")
        return out

    @model_validator(mode="after")
    def _validate_worker_pool_headroom(self) -> Self:
        """Fail fast if the worker DB pool can't serve all concurrent sends.

        Each in-flight send finalizes in its own transaction (one pooled
        connection), concurrently with the heartbeat and reaper. If
        ``worker_db_pool_size`` is smaller than ``worker_send_concurrency``
        plus :data:`WORKER_DB_POOL_HEADROOM`, finalizers stall waiting for a
        connection and can time out — turning a successful send into a
        duplicate once the reaper reclaims the still-``processing`` job. Surface
        the misconfiguration at startup rather than under load.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If the pool is too small for the configured concurrency.
        """
        required = self.worker_send_concurrency + WORKER_DB_POOL_HEADROOM
        if self.worker_db_pool_size < required:
            raise ValueError(
                f"worker_db_pool_size ({self.worker_db_pool_size}) must be >= "
                f"worker_send_concurrency ({self.worker_send_concurrency}) + "
                f"{WORKER_DB_POOL_HEADROOM} headroom (= {required}) for the "
                "claim, heartbeat, and reaper queries"
            )
        return self


settings = Settings()

TORTOISE_ORM: dict = {
    "connections": {
        "default": settings.db_url,
    },
    "apps": {
        "models": {
            "models": ["hefest.models"],
            "default_connection": "default",
            "migrations": "migrations",
        },
    },
}


def build_worker_tortoise_orm() -> dict:
    """Build the notification worker's own Tortoise config.

    Identical to ``TORTOISE_ORM`` except the connection's asyncpg pool
    ``maxsize`` is sized for the worker's concurrent claim/send/heartbeat/
    reaper load (``settings.worker_db_pool_size``) instead of the shared
    web-request pool default.

    The asyncpg client (tortoise-orm 1.1.7) reads the pool size via
    ``self.extra.pop("maxsize", 5)`` in ``BasePostgresClient.__init__``,
    where ``extra`` is populated from any DSN query-string parameter not
    otherwise consumed — so appending ``?maxsize=N`` to the connection URL
    sets the asyncpg pool's ``max_size`` correctly (verified against
    ``tortoise.backends.base_postgres.client`` source and a live connection).
    A separator of ``&`` vs ``?`` is chosen based on whether ``db_url``
    already has a query string.
    """
    separator = "&" if "?" in settings.db_url else "?"
    pool_size = settings.worker_db_pool_size
    worker_db_url = f"{settings.db_url}{separator}maxsize={pool_size}"
    return {
        "connections": {
            "default": worker_db_url,
        },
        "apps": {
            "models": {
                "models": ["hefest.models"],
                "default_connection": "default",
                "migrations": "migrations",
            },
        },
    }
