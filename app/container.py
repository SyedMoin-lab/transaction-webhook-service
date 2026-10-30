from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.repository import TransactionRepository
from app.worker import TransactionWorker


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    settings: Settings
    engine: AsyncEngine
    repository: TransactionRepository
    worker: TransactionWorker | None
