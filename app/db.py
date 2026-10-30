from typing import Any
from uuid import uuid4

from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings

ASYNC_DRIVER = "postgresql+asyncpg"
POSTGRES_DRIVER_ALIASES = frozenset(
    {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg", "postgresql+psycopg2"}
)
LIBPQ_ONLY_PARAMETERS = ("sslmode", "channel_binding", "ssl")
SSL_ALIASES = {"true": "require", "1": "require", "false": "disable", "0": "disable"}


def _prepared_statement_name() -> str:
    return f"__asyncpg_{uuid4().hex}__"


def build_connection_options(settings: Settings) -> tuple[URL, dict[str, Any]]:
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername not in POSTGRES_DRIVER_ALIASES:
        raise ValueError(f"Unsupported database driver: {url.drivername}")

    connect_args: dict[str, Any] = {
        "command_timeout": settings.database_command_timeout_seconds,
        "timeout": settings.database_connect_timeout_seconds,
        "server_settings": {"application_name": settings.app_name},
    }

    ssl_value = url.query.get("sslmode") or url.query.get("ssl")
    if isinstance(ssl_value, tuple):
        ssl_value = ssl_value[-1]
    if ssl_value:
        connect_args["ssl"] = SSL_ALIASES.get(ssl_value.lower(), ssl_value.lower())

    if settings.database_pgbouncer_mode:
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = _prepared_statement_name

    url = url.set(drivername=ASYNC_DRIVER).difference_update_query(LIBPQ_ONLY_PARAMETERS)
    return url, connect_args


def create_engine(settings: Settings, *, pooled: bool = True) -> AsyncEngine:
    url, connect_args = build_connection_options(settings)
    if not pooled:
        return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    return create_async_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_recycle=settings.database_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
