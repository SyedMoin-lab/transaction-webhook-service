import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import Transaction

pytestmark = pytest.mark.integration


async def test_application_engine_commits_each_statement_without_a_transaction(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        await connection.execute(
            insert(Transaction).values(
                transaction_id="txn_autocommit",
                source_account="acc_a",
                destination_account="acc_b",
                amount=10,
                currency="INR",
            )
        )

    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count()).where(Transaction.transaction_id == "txn_autocommit")
        )

    assert count == 1
