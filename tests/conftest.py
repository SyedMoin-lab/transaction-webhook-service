from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.engine import create_engine
from app.repositories.transactions import TransactionRepository
from tests.support import build_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def migrated_database() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = build_settings()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
async def engine(migrated_database: None, settings: Settings) -> AsyncIterator[AsyncEngine]:
    database_engine = create_engine(settings)
    async with database_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE transactions"))
    yield database_engine
    await database_engine.dispose()


@pytest.fixture
def repository(engine: AsyncEngine) -> TransactionRepository:
    return TransactionRepository(engine)
