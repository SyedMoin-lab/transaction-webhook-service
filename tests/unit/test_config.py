from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import ASYNC_DRIVER, build_connection_options
from tests.support import build_settings


def test_defaults_match_assessment_requirements() -> None:
    settings = Settings(_env_file=None)

    assert settings.processing_delay_seconds == 30.0
    assert settings.worker_enabled is True
    assert settings.worker_lease_seconds > settings.processing_delay_seconds
    assert settings.webhook_secret is None


def test_env_local_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PROCESSING_DELAY_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://u:p@from-env/app\nPROCESSING_DELAY_SECONDS=12\n"
    )
    (tmp_path / ".env.local").write_text("DATABASE_URL=postgresql://u:p@from-env-local/app\n")

    settings = Settings()

    assert settings.database_url.get_secret_value().endswith("@from-env-local/app")
    assert settings.processing_delay_seconds == 12


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_webhook_secret_disables_signature_verification(value: str) -> None:
    assert build_settings(webhook_secret=value).webhook_secret is None


def test_webhook_secret_is_kept_secret() -> None:
    settings = build_settings(webhook_secret="whsec_live")

    assert settings.webhook_secret is not None
    assert settings.webhook_secret.get_secret_value() == "whsec_live"
    assert "whsec_live" not in repr(settings)


def test_production_requires_a_non_local_database() -> None:
    with pytest.raises(ValidationError, match="managed database"):
        build_settings(
            environment="production",
            database_url="postgresql://postgres:postgres@localhost:5432/app",
        )

    settings = build_settings(
        environment="production", database_url="postgresql://u:p@ep-example.neon.tech/app"
    )
    assert settings.environment == "production"


def test_lease_must_exceed_processing_delay() -> None:
    with pytest.raises(ValidationError, match="worker_lease_seconds must be greater"):
        build_settings(processing_delay_seconds=30, worker_lease_seconds=30)


def test_retry_ceiling_must_not_be_below_base() -> None:
    with pytest.raises(ValidationError, match="worker_retry_max_seconds"):
        build_settings(worker_retry_base_seconds=10, worker_retry_max_seconds=1)


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgres://user:secret@db.example.com:5432/app",
        "postgresql://user:secret@db.example.com:5432/app",
        "postgresql+psycopg://user:secret@db.example.com:5432/app",
        "postgresql+asyncpg://user:secret@db.example.com:5432/app",
    ],
)
def test_postgres_urls_are_normalised_to_asyncpg(raw_url: str) -> None:
    url, _ = build_connection_options(build_settings(database_url=raw_url))

    assert url.drivername == ASYNC_DRIVER
    assert url.host == "db.example.com"
    assert url.database == "app"
    assert url.password == "secret"


def test_libpq_ssl_parameters_are_translated_for_asyncpg() -> None:
    settings = build_settings(
        database_url=(
            "postgresql://user:secret@ep-example.neon.tech/app"
            "?sslmode=require&channel_binding=require"
        )
    )

    url, connect_args = build_connection_options(settings)

    assert dict(url.query) == {}
    assert connect_args["ssl"] == "require"


@pytest.mark.parametrize(("value", "expected"), [("true", "require"), ("false", "disable")])
def test_boolean_ssl_flags_are_translated(value: str, expected: str) -> None:
    settings = build_settings(database_url=f"postgresql://u:p@host/app?ssl={value}")

    _, connect_args = build_connection_options(settings)

    assert connect_args["ssl"] == expected


def test_connect_args_include_timeouts_and_application_name() -> None:
    settings = build_settings(
        database_command_timeout_seconds=7, database_connect_timeout_seconds=3
    )

    _, connect_args = build_connection_options(settings)

    assert connect_args["command_timeout"] == 7
    assert connect_args["timeout"] == 3
    assert connect_args["server_settings"] == {"application_name": settings.app_name}
    assert "ssl" not in connect_args
    assert "statement_cache_size" not in connect_args


def test_pgbouncer_mode_disables_prepared_statement_caching() -> None:
    settings = build_settings(database_pgbouncer_mode=True)

    _, connect_args = build_connection_options(settings)

    assert connect_args["statement_cache_size"] == 0
    assert connect_args["prepared_statement_cache_size"] == 0
    name_factory = connect_args["prepared_statement_name_func"]
    assert name_factory() != name_factory()


def test_unsupported_database_driver_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported database driver"):
        build_connection_options(build_settings(database_url="mysql://u:p@host/app"))
