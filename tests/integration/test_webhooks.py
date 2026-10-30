import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.models import Transaction
from app.repository import TransactionRepository
from tests.support import (
    UNREACHABLE_DATABASE_URL,
    WEBHOOK_PATH,
    build_settings,
    running_app,
    webhook_payload,
)

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "whsec_test_secret"


async def count_transactions(engine: AsyncEngine) -> int:
    statement = select(func.count()).select_from(Transaction)
    async with engine.connect() as connection:
        return int((await connection.execute(statement)).scalar_one())


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_is_acknowledged_with_202(
    engine: AsyncEngine, repository: TransactionRepository
) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        response = await client.post(WEBHOOK_PATH, json=webhook_payload())

    assert response.status_code == 202
    assert response.json() == {
        "status": "ACCEPTED",
        "transaction_id": "txn_abc123def456",
        "duplicate": False,
    }
    record = await repository.get("txn_abc123def456")
    assert record is not None
    assert record.status == "PROCESSING"
    assert record.attempts == 0
    assert record.processed_at is None


async def test_duplicate_webhooks_are_acknowledged_without_duplicate_rows(
    engine: AsyncEngine,
) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        first = await client.post(WEBHOOK_PATH, json=webhook_payload())
        second = await client.post(WEBHOOK_PATH, json=webhook_payload())
        third = await client.post(WEBHOOK_PATH, json=webhook_payload())

    assert [first.status_code, second.status_code, third.status_code] == [202, 202, 202]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert third.json()["duplicate"] is True
    assert await count_transactions(engine) == 1


async def test_concurrent_duplicate_webhooks_create_exactly_one_transaction(
    engine: AsyncEngine,
) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        responses = await asyncio.gather(
            *(client.post(WEBHOOK_PATH, json=webhook_payload()) for _ in range(30))
        )

    assert all(response.status_code == 202 for response in responses)
    assert sum(not response.json()["duplicate"] for response in responses) == 1
    assert await count_transactions(engine) == 1


async def test_first_delivery_wins_when_duplicate_payload_differs(
    repository: TransactionRepository,
) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        await client.post(WEBHOOK_PATH, json=webhook_payload(amount=1500))
        response = await client.post(WEBHOOK_PATH, json=webhook_payload(amount=9999))

    assert response.status_code == 202
    assert response.json()["duplicate"] is True
    record = await repository.get("txn_abc123def456")
    assert record is not None
    assert record.amount == 1500


async def test_currency_is_stored_normalised(repository: TransactionRepository) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        await client.post(WEBHOOK_PATH, json=webhook_payload(currency="inr"))

    record = await repository.get("txn_abc123def456")
    assert record is not None
    assert record.currency == "INR"


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": -5},
        {"amount": 0},
        {"amount": "not-a-number"},
        {"currency": "RUPEES"},
        {"transaction_id": ""},
        {"source_account": None},
    ],
)
@pytest.mark.usefixtures("engine")
async def test_invalid_payloads_are_rejected_with_422(overrides: dict[str, Any]) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        response = await client.post(WEBHOOK_PATH, json=webhook_payload(**overrides))

    assert response.status_code == 422
    assert "detail" in response.json()


@pytest.mark.usefixtures("engine")
async def test_malformed_json_is_rejected_with_422() -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        response = await client.post(
            WEBHOOK_PATH, content=b"{not json", headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 422


@pytest.mark.usefixtures("engine")
async def test_webhook_returns_503_when_database_is_unavailable() -> None:
    settings = build_settings(
        database_url=UNREACHABLE_DATABASE_URL,
        worker_enabled=False,
        database_connect_timeout_seconds=1,
    )
    async with running_app(settings) as (_, client):
        response = await client.post(WEBHOOK_PATH, json=webhook_payload())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {"detail": "Service temporarily unavailable, please retry"}


@pytest.mark.usefixtures("engine")
async def test_webhook_responds_quickly_even_with_a_30_second_processing_delay(
    settings: Settings,
) -> None:
    slow_settings = build_settings(processing_delay_seconds=30, worker_lease_seconds=120)
    async with running_app(slow_settings) as (_, client):
        started = time.perf_counter()
        response = await client.post(WEBHOOK_PATH, json=webhook_payload())
        elapsed = time.perf_counter() - started

        assert response.status_code == 202
        assert elapsed < 0.5
        status = await client.get("/v1/transactions/txn_abc123def456")
        assert status.json()[0]["status"] == "PROCESSING"


class TestSignatureVerification:
    @pytest.fixture
    def signed_settings(self) -> Settings:
        return build_settings(worker_enabled=False, webhook_secret=WEBHOOK_SECRET)

    @pytest.mark.usefixtures("engine")
    async def test_missing_signature_is_rejected(self, signed_settings: Settings) -> None:
        async with running_app(signed_settings) as (_, client):
            response = await client.post(WEBHOOK_PATH, json=webhook_payload())

        assert response.status_code == 401
        assert response.json() == {"detail": "Missing webhook signature"}

    @pytest.mark.usefixtures("engine")
    async def test_invalid_signature_is_rejected(self, signed_settings: Settings) -> None:
        body = json.dumps(webhook_payload()).encode()
        async with running_app(signed_settings) as (_, client):
            response = await client.post(
                WEBHOOK_PATH,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": sign(body, "wrong-secret"),
                },
            )

        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid webhook signature"}

    @pytest.mark.parametrize("prefix", ["", "sha256="])
    async def test_valid_signature_is_accepted(
        self, signed_settings: Settings, engine: AsyncEngine, prefix: str
    ) -> None:
        body = json.dumps(webhook_payload()).encode()
        async with running_app(signed_settings) as (_, client):
            response = await client.post(
                WEBHOOK_PATH,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": f"{prefix}{sign(body)}",
                },
            )

        assert response.status_code == 202
        assert await count_transactions(engine) == 1
