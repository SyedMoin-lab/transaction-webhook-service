import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.config import Settings
from app.db.engine import build_connection_options, create_engine
from app.db.models import Base

MIGRATION_LOCK_KEY = 7_265_031_942

config = context.config
target_metadata = Base.metadata


def resolve_settings() -> Settings:
    override = config.attributes.get("settings")
    if isinstance(override, Settings):
        return override
    return Settings()


def run_migrations_offline() -> None:
    url, _ = build_connection_options(resolve_settings())
    context.configure(
        url=url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_with_lock(connection: Connection) -> None:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(resolve_settings(), pooled=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(run_migrations_with_lock)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
