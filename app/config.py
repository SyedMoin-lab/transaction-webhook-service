from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url

LOCAL_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "transaction-webhook-service"
    app_version: str = "1.0.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    docs_enabled: bool = True

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/transactions"
    )
    database_pool_size: int = Field(default=20, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    database_pool_recycle_seconds: int = Field(default=1800, gt=0)
    database_command_timeout_seconds: float = Field(default=10.0, gt=0)
    database_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    database_pgbouncer_mode: bool = False

    webhook_persist_timeout_seconds: float = Field(default=2.0, gt=0)
    webhook_secret: SecretStr | None = None
    webhook_signature_header: str = "X-Webhook-Signature"

    health_check_timeout_seconds: float = Field(default=2.0, gt=0)

    processing_delay_seconds: float = Field(default=30.0, ge=0)

    worker_enabled: bool = True
    worker_concurrency: int = Field(default=100, ge=1)
    worker_batch_size: int = Field(default=25, ge=1)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: float = Field(default=120.0, gt=0)
    worker_max_attempts: int = Field(default=5, ge=1)
    worker_retry_base_seconds: float = Field(default=2.0, gt=0)
    worker_retry_max_seconds: float = Field(default=300.0, gt=0)
    worker_shutdown_grace_seconds: float = Field(default=5.0, ge=0)

    @field_validator("webhook_secret", mode="before")
    @classmethod
    def blank_secret_disables_verification(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_worker_timing(self) -> Self:
        if self.worker_lease_seconds <= self.processing_delay_seconds:
            raise ValueError("worker_lease_seconds must be greater than processing_delay_seconds")
        if self.worker_retry_max_seconds < self.worker_retry_base_seconds:
            raise ValueError("worker_retry_max_seconds must be at least worker_retry_base_seconds")
        return self

    @model_validator(mode="after")
    def validate_production_database(self) -> Self:
        if self.environment != "production":
            return self
        host = make_url(self.database_url.get_secret_value()).host
        if host is None or host in LOCAL_DATABASE_HOSTS:
            raise ValueError("DATABASE_URL must point to a managed database in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
