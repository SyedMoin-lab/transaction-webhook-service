import asyncio
from typing import Protocol

from app.repositories.transactions import TransactionRecord


class TransactionProcessor(Protocol):
    async def process(self, transaction: TransactionRecord) -> None: ...


class SimulatedExternalProcessor:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    async def process(self, transaction: TransactionRecord) -> None:
        await asyncio.sleep(self._delay_seconds)
