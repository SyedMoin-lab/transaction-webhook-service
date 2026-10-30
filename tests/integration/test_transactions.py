from datetime import datetime

import pytest
from httpx import AsyncClient

from app.repository import TransactionRecord, TransactionRepository
from tests.support import WEBHOOK_PATH, build_settings, running_app, wait_until, webhook_payload

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("engine")]

STATUS_PATH = "/v1/transactions/{transaction_id}"


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


async def fetch_status(client: AsyncClient, transaction_id: str) -> list[dict[str, object]]:
    response = await client.get(STATUS_PATH.format(transaction_id=transaction_id))
    assert response.status_code == 200
    body: list[dict[str, object]] = response.json()
    return body


async def test_unknown_transaction_returns_404() -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        response = await client.get(STATUS_PATH.format(transaction_id="txn_missing"))

    assert response.status_code == 404
    assert response.json() == {"detail": "Transaction not found"}


async def test_overlong_transaction_id_is_rejected() -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        response = await client.get(STATUS_PATH.format(transaction_id="x" * 256))

    assert response.status_code == 422


async def test_pending_transaction_is_reported_as_processing() -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        await client.post(WEBHOOK_PATH, json=webhook_payload())
        body = await fetch_status(client, "txn_abc123def456")

    assert len(body) == 1
    transaction = body[0]
    assert set(transaction) == {
        "transaction_id",
        "source_account",
        "destination_account",
        "amount",
        "currency",
        "status",
        "created_at",
        "processed_at",
    }
    assert transaction["transaction_id"] == "txn_abc123def456"
    assert transaction["source_account"] == "acc_user_789"
    assert transaction["destination_account"] == "acc_merchant_456"
    assert transaction["amount"] == 1500
    assert transaction["currency"] == "INR"
    assert transaction["status"] == "PROCESSING"
    assert transaction["processed_at"] is None
    parse_timestamp(str(transaction["created_at"]))


async def test_transaction_is_processed_after_the_configured_delay() -> None:
    delay = 0.5
    settings = build_settings(processing_delay_seconds=delay)
    async with running_app(settings) as (_, client):
        response = await client.post(
            WEBHOOK_PATH, json=webhook_payload(amount=150.50, currency="USD")
        )
        assert response.status_code == 202
        assert (await fetch_status(client, "txn_abc123def456"))[0]["status"] == "PROCESSING"

        async def is_processed() -> bool:
            body = await fetch_status(client, "txn_abc123def456")
            return body[0]["status"] == "PROCESSED"

        await wait_until(is_processed, within=5)
        transaction = (await fetch_status(client, "txn_abc123def456"))[0]

    assert transaction["amount"] == 150.5
    assert transaction["currency"] == "USD"
    assert transaction["processed_at"] is not None
    created_at = parse_timestamp(str(transaction["created_at"]))
    processed_at = parse_timestamp(str(transaction["processed_at"]))
    assert processed_at >= created_at


async def test_unexpected_errors_return_a_generic_500(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(self: TransactionRepository, transaction_id: str) -> TransactionRecord:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(TransactionRepository, "get", explode)
    settings = build_settings(worker_enabled=False)
    async with running_app(settings, raise_app_exceptions=False) as (_, client):
        response = await client.get(STATUS_PATH.format(transaction_id="txn_any"))

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret" not in response.text
