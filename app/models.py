from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

IDENTIFIER_MAX_LENGTH = 255
CURRENCY_CODE_LENGTH = 3
AMOUNT_PRECISION = 20
AMOUNT_SCALE = 4

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TransactionStatus(StrEnum):
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSING', 'PROCESSED', 'FAILED')",
            name="status_valid",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index(
            "ix_transactions_due",
            "next_attempt_at",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
    )

    transaction_id: Mapped[str] = mapped_column(String(IDENTIFIER_MAX_LENGTH), primary_key=True)
    source_account: Mapped[str] = mapped_column(String(IDENTIFIER_MAX_LENGTH))
    destination_account: Mapped[str] = mapped_column(String(IDENTIFIER_MAX_LENGTH))
    amount: Mapped[Decimal] = mapped_column(Numeric(AMOUNT_PRECISION, AMOUNT_SCALE))
    currency: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH))
    status: Mapped[str] = mapped_column(
        String(16), server_default=TransactionStatus.PROCESSING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
