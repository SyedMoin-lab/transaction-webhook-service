from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
)

from app.models import (
    AMOUNT_PRECISION,
    AMOUNT_SCALE,
    CURRENCY_CODE_LENGTH,
    IDENTIFIER_MAX_LENGTH,
    TransactionStatus,
)

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(UTC_TIMESTAMP_FORMAT)


def serialize_amount(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=IDENTIFIER_MAX_LENGTH),
]
UtcTimestamp = Annotated[datetime, PlainSerializer(format_utc_timestamp, return_type=str)]
AmountValue = Annotated[
    Decimal,
    PlainSerializer(serialize_amount, return_type=int | float, when_used="json"),
]


class TransactionWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    transaction_id: Identifier
    source_account: Identifier
    destination_account: Identifier
    amount: Decimal = Field(
        gt=0,
        max_digits=AMOUNT_PRECISION,
        decimal_places=AMOUNT_SCALE,
        allow_inf_nan=False,
    )
    currency: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            to_upper=True,
            min_length=CURRENCY_CODE_LENGTH,
            max_length=CURRENCY_CODE_LENGTH,
            pattern=r"^[A-Za-z]{3}$",
        ),
    ]

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("amount must be a number")
        return value


class WebhookAcknowledgement(BaseModel):
    status: Literal["ACCEPTED"] = "ACCEPTED"
    transaction_id: str
    duplicate: bool


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    source_account: str
    destination_account: str
    amount: AmountValue
    currency: str
    status: TransactionStatus
    created_at: UtcTimestamp
    processed_at: UtcTimestamp | None


class HealthResponse(BaseModel):
    status: Literal["HEALTHY", "UNHEALTHY"]
    current_time: UtcTimestamp


class ErrorResponse(BaseModel):
    detail: str
