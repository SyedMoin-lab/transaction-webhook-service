import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.processor import TransactionProcessor
from app.repository import TransactionRecord

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/transactions_test",
)
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:1/unreachable"

WEBHOOK_PATH = "/v1/webhooks/transactions"


def build_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "environment": "test",
        "log_json": False,
        "log_level": "WARNING",
        "docs_enabled": True,
        "processing_delay_seconds": 0.2,
        "worker_enabled": True,
        "worker_poll_interval_seconds": 0.05,
        "worker_lease_seconds": 5.0,
        "worker_retry_base_seconds": 0.05,
        "worker_retry_max_seconds": 0.2,
        "worker_shutdown_grace_seconds": 0.0,
        "database_pool_size": 20,
        "database_max_overflow": 10,
        "webhook_secret": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def webhook_payload(transaction_id: str = "txn_abc123def456", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    payload.update(overrides)
    return payload


@asynccontextmanager
async def running_app(
    settings: Settings,
    processor: TransactionProcessor | None = None,
    *,
    raise_app_exceptions: bool = True,
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(settings, processor)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield app, client


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    within: float = 5.0,
    interval: float = 0.05,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not await predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"Condition not met within {within} seconds")
        await asyncio.sleep(interval)


class RecordingProcessor:
    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.calls: list[str] = []

    async def process(self, transaction: TransactionRecord) -> None:
        self.calls.append(transaction.transaction_id)
        await asyncio.sleep(self.delay_seconds)


class FlakyProcessor:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def process(self, transaction: TransactionRecord) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"Simulated upstream failure #{self.calls}")


class BlockingProcessor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process(self, transaction: TransactionRecord) -> None:
        self.started.set()
        await self.release.wait()
