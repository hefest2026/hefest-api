from __future__ import annotations

from typing import Final

from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_OAUTH_PROVIDERS: Final = ("google", "microsoft")


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

    # relay
    relay_poll_interval: float = 1.0

    # rate limiting
    rate_limit_login_count: int = 10
    rate_limit_login_window: int = 60
    rate_limit_register_count: int = 5
    rate_limit_register_window: int = 3600
    rate_limit_event_register_count: int = 30
    rate_limit_event_register_window: int = 60
    rate_limit_global_count: int = 200
    rate_limit_global_window: int = 60

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
