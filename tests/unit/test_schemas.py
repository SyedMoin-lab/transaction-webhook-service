from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import TransactionStatus
from app.schemas import (
    HealthResponse,
    TransactionResponse,
    TransactionWebhook,
    format_utc_timestamp,
    serialize_amount,
)
from tests.support import webhook_payload


def test_valid_webhook_is_parsed() -> None:
    webhook = TransactionWebhook.model_validate(webhook_payload())

    assert webhook.transaction_id == "txn_abc123def456"
    assert webhook.source_account == "acc_user_789"
    assert webhook.destination_account == "acc_merchant_456"
    assert webhook.amount == Decimal("1500")
    assert webhook.currency == "INR"


def test_webhook_normalises_currency_and_whitespace() -> None:
    webhook = TransactionWebhook.model_validate(
        webhook_payload(transaction_id="  txn_1  ", currency=" usd ")
    )

    assert webhook.transaction_id == "txn_1"
    assert webhook.currency == "USD"


def test_webhook_accepts_decimal_amounts_and_numeric_strings() -> None:
    assert TransactionWebhook.model_validate(webhook_payload(amount=150.50)).amount == Decimal(
        "150.5"
    )
    assert TransactionWebhook.model_validate(webhook_payload(amount="99.99")).amount == Decimal(
        "99.99"
    )


def test_webhook_ignores_unknown_fields() -> None:
    webhook = TransactionWebhook.model_validate(webhook_payload(event="payment.captured"))

    assert "event" not in webhook.model_dump()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", 0),
        ("amount", -10),
        ("amount", "abc"),
        ("amount", True),
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("amount", 1.23456),
        ("amount", 10**17),
        ("currency", "IN"),
        ("currency", "INRR"),
        ("currency", "12A"),
        ("currency", ""),
        ("transaction_id", ""),
        ("transaction_id", "   "),
        ("transaction_id", "x" * 256),
        ("source_account", ""),
        ("destination_account", None),
    ],
)
def test_invalid_webhook_values_are_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        TransactionWebhook.model_validate(webhook_payload(**{field: value}))


@pytest.mark.parametrize(
    "missing", ["transaction_id", "source_account", "destination_account", "amount", "currency"]
)
def test_missing_webhook_fields_are_rejected(missing: str) -> None:
    payload = webhook_payload()
    payload.pop(missing)

    with pytest.raises(ValidationError):
        TransactionWebhook.model_validate(payload)


def test_timestamps_are_formatted_as_utc_with_z_suffix() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))

    assert format_utc_timestamp(datetime(2024, 1, 15, 10, 30, 0, 123456, UTC)) == (
        "2024-01-15T10:30:00Z"
    )
    assert format_utc_timestamp(datetime(2024, 1, 15, 16, 0, 30, tzinfo=ist)) == (
        "2024-01-15T10:30:30Z"
    )
    assert format_utc_timestamp(datetime(2024, 1, 15, 10, 30, 0)) == "2024-01-15T10:30:00Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1500.0000"), 1500),
        (Decimal("150.5000"), 150.5),
        (Decimal("0.0100"), 0.01),
    ],
)
def test_amount_serialisation(value: Decimal, expected: float) -> None:
    serialised = serialize_amount(value)

    assert serialised == expected
    assert type(serialised) is type(expected)


def test_transaction_response_matches_specified_shape() -> None:
    response = TransactionResponse(
        transaction_id="txn_abc123def456",
        source_account="acc_user_789",
        destination_account="acc_merchant_456",
        amount=Decimal("150.5000"),
        currency="USD",
        status=TransactionStatus.PROCESSED,
        created_at=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        processed_at=datetime(2024, 1, 15, 10, 30, 30, tzinfo=UTC),
    )

    assert response.model_dump(mode="json") == {
        "transaction_id": "txn_abc123def456",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 150.5,
        "currency": "USD",
        "status": "PROCESSED",
        "created_at": "2024-01-15T10:30:00Z",
        "processed_at": "2024-01-15T10:30:30Z",
    }


def test_processing_transaction_serialises_null_processed_at() -> None:
    response = TransactionResponse(
        transaction_id="txn_1",
        source_account="acc_a",
        destination_account="acc_b",
        amount=Decimal("1500"),
        currency="INR",
        status=TransactionStatus.PROCESSING,
        created_at=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        processed_at=None,
    )

    dumped = response.model_dump(mode="json")

    assert dumped["status"] == "PROCESSING"
    assert dumped["processed_at"] is None
    assert dumped["amount"] == 1500


def test_health_response_shape() -> None:
    response = HealthResponse(
        status="HEALTHY", current_time=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    )

    assert response.model_dump(mode="json") == {
        "status": "HEALTHY",
        "current_time": "2024-01-15T10:30:00Z",
    }
