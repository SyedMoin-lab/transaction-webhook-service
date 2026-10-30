import asyncio
import signal

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.engine import create_engine
from app.repositories.transactions import TransactionRepository
from app.workers.transaction_worker import TransactionWorker


async def run_standalone_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings)
    engine = create_engine(settings)
    worker = TransactionWorker.from_settings(settings, TransactionRepository(engine))
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, shutdown.set)
    try:
        await worker.start()
        await shutdown.wait()
    finally:
        await worker.stop()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_standalone_worker())
