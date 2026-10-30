from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.dml import ReturningUpdate

from app.models import Transaction, TransactionStatus
from app.schemas import TransactionWebhook

ERROR_MESSAGE_MAX_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: TransactionStatus
    attempts: int
    last_error: str | None
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None

    @classmethod
    def from_row(cls, row: Row[Any]) -> "TransactionRecord":
        mapping = row._mapping
        return cls(
            transaction_id=mapping["transaction_id"],
            source_account=mapping["source_account"],
            destination_account=mapping["destination_account"],
            amount=mapping["amount"],
            currency=mapping["currency"],
            status=TransactionStatus(mapping["status"]),
            attempts=mapping["attempts"],
            last_error=mapping["last_error"],
            next_attempt_at=mapping["next_attempt_at"],
            created_at=mapping["created_at"],
            updated_at=mapping["updated_at"],
            processed_at=mapping["processed_at"],
        )


TRANSACTION_COLUMNS = tuple(Transaction.__table__.columns)


class TransactionRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine.execution_options(isolation_level="AUTOCOMMIT")

    async def create_if_absent(self, webhook: TransactionWebhook) -> bool:
        statement = (
            insert(Transaction)
            .values(
                transaction_id=webhook.transaction_id,
                source_account=webhook.source_account,
                destination_account=webhook.destination_account,
                amount=webhook.amount,
                currency=webhook.currency,
            )
            .on_conflict_do_nothing(index_elements=[Transaction.transaction_id])
            .returning(Transaction.transaction_id)
        )
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            return result.scalar_one_or_none() is not None

    async def get(self, transaction_id: str) -> TransactionRecord | None:
        statement = select(*TRANSACTION_COLUMNS).where(Transaction.transaction_id == transaction_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).one_or_none()
        return TransactionRecord.from_row(row) if row is not None else None

    async def claim_due(self, *, limit: int, lease_seconds: float) -> list[TransactionRecord]:
        due = (
            select(Transaction.transaction_id)
            .where(
                Transaction.status == TransactionStatus.PROCESSING.value,
                Transaction.next_attempt_at <= func.now(),
            )
            .order_by(Transaction.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("due")
        )
        statement = (
            update(Transaction)
            .where(Transaction.transaction_id == due.c.transaction_id)
            .values(
                attempts=Transaction.attempts + 1,
                next_attempt_at=func.now() + timedelta(seconds=lease_seconds),
                updated_at=func.now(),
            )
            .returning(*TRANSACTION_COLUMNS)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return sorted(
            (TransactionRecord.from_row(row) for row in rows),
            key=lambda record: record.created_at,
        )

    async def mark_processed(self, transaction_id: str, attempt: int) -> bool:
        statement = (
            update(Transaction)
            .where(
                Transaction.transaction_id == transaction_id,
                Transaction.status == TransactionStatus.PROCESSING.value,
                Transaction.attempts == attempt,
            )
            .values(
                status=TransactionStatus.PROCESSED.value,
                processed_at=func.now(),
                updated_at=func.now(),
                last_error=None,
            )
            .returning(Transaction.transaction_id)
        )
        return await self._execute_fenced(statement)

    async def schedule_retry(
        self, transaction_id: str, attempt: int, error: str, delay_seconds: float
    ) -> bool:
        statement = (
            update(Transaction)
            .where(
                Transaction.transaction_id == transaction_id,
                Transaction.status == TransactionStatus.PROCESSING.value,
                Transaction.attempts == attempt,
            )
            .values(
                next_attempt_at=func.now() + timedelta(seconds=delay_seconds),
                updated_at=func.now(),
                last_error=error[:ERROR_MESSAGE_MAX_LENGTH],
            )
            .returning(Transaction.transaction_id)
        )
        return await self._execute_fenced(statement)

    async def mark_failed(self, transaction_id: str, attempt: int, error: str) -> bool:
        statement = (
            update(Transaction)
            .where(
                Transaction.transaction_id == transaction_id,
                Transaction.status == TransactionStatus.PROCESSING.value,
                Transaction.attempts == attempt,
            )
            .values(
                status=TransactionStatus.FAILED.value,
                updated_at=func.now(),
                last_error=error[:ERROR_MESSAGE_MAX_LENGTH],
            )
            .returning(Transaction.transaction_id)
        )
        return await self._execute_fenced(statement)

    async def release(self, transaction_id: str, attempt: int) -> bool:
        statement = (
            update(Transaction)
            .where(
                Transaction.transaction_id == transaction_id,
                Transaction.status == TransactionStatus.PROCESSING.value,
                Transaction.attempts == attempt,
            )
            .values(
                attempts=Transaction.attempts - 1,
                next_attempt_at=func.now(),
                updated_at=func.now(),
            )
            .returning(Transaction.transaction_id)
        )
        return await self._execute_fenced(statement)

    async def _execute_fenced(self, statement: ReturningUpdate[str]) -> bool:
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            return result.scalar_one_or_none() is not None
