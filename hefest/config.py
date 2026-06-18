from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables (prefix HEFEST_)."""

    model_config = SettingsConfigDict(env_prefix="HEFEST_", env_file=".env")

    env: str = "dev"
    log_level: str = "DEBUG"

    db_url: str = "asyncpg://hefest:hefest@localhost:5432/hefest_db"
    redis_url: str = "redis://localhost:6379"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # relay — outbox-to-Redis bridge.
    #
    # The relay is push-driven via PostgreSQL LISTEN/NOTIFY (channel below): an
    # AFTER INSERT trigger on notification_jobs fires NOTIFY at COMMIT, waking the
    # relay in ~milliseconds instead of waiting for a poll tick. NOTIFY is
    # fire-and-forget (at-most-once) — a signal emitted while the relay is
    # disconnected is lost — so a long-interval fallback poll guarantees eventual
    # drain and catch-up after downtime. The outbox row itself is the durable
    # source of truth; NOTIFY only collapses latency. See worker/relay.py.
    relay_notify_channel: str = "hefest_jobs"
    relay_fallback_poll_interval: float = 5.0
    """Safety-net poll cadence (seconds) for jobs whose NOTIFY was missed."""
    relay_batch_size: int = 100
    """Max pending rows claimed per drain pass (FOR UPDATE SKIP LOCKED)."""
    relay_stream: str = "hefest:notifications"
    relay_stream_maxlen: int = 10_000
    """Approximate (~) cap on Redis stream length via XADD MAXLEN."""

    # rate limiting
    rate_limit_login_count: int = 10
    rate_limit_login_window: int = 60
    rate_limit_register_count: int = 5
    rate_limit_register_window: int = 3600
    rate_limit_event_register_count: int = 30
    rate_limit_event_register_window: int = 60
    rate_limit_global_count: int = 200
    rate_limit_global_window: int = 60


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
