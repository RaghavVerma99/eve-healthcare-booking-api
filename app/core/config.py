from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "EVE Diagnostics Booking API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://eve:eve@localhost:5432/eve_diagnostics"
    test_database_url: str = "sqlite+aiosqlite:///./test.db"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    jwt_secret_key: str = "change-me-in-production-use-a-long-random-value"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    webhook_signing_secret: str = "mockpay-webhook-secret"
    webhook_require_signature: bool = False

    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 60

    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    log_level: str = "INFO"
    log_json: bool = True

    seed_admin_email: str = "admin@eve.health"
    seed_admin_password: str = "Admin@12345"

    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "").replace("+aiosqlite", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
