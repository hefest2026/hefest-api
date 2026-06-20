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

    # relay
    relay_poll_interval_seconds: float = 1.0

    # trusted reverse proxies (ProxyHeadersMiddleware uses this list)
    # set to the nginx container IP or CIDR in production
    trusted_proxies: list[str] = ["127.0.0.1", "::1"]

    # rate limiting
    rate_limit_login_count: int = 10
    rate_limit_login_window_seconds: int = 60
    rate_limit_register_count: int = 5
    rate_limit_register_window_seconds: int = 3600
    rate_limit_event_register_count: int = 30
    rate_limit_event_register_window_seconds: int = 60
    rate_limit_global_count: int = 200
    rate_limit_global_window_seconds: int = 60


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
