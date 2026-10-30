from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, PlainSerializer, StringConstraints

from app.db.models import IDENTIFIER_MAX_LENGTH

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(UTC_TIMESTAMP_FORMAT)


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=IDENTIFIER_MAX_LENGTH),
]
UtcTimestamp = Annotated[datetime, PlainSerializer(format_utc_timestamp, return_type=str)]


class ErrorResponse(BaseModel):
    detail: str
