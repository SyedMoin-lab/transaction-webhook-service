import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import Settings
from app.repositories.transactions import TransactionRecord, TransactionRepository
from app.services.processor import SimulatedExternalProcessor, TransactionProcessor

logger = logging.getLogger(__name__)

PERSIST_RETRY_DELAYS_SECONDS = (0.25, 1.0, 2.0)
CLAIM_ERROR_BACKOFF_MAX_SECONDS = 30.0


class TransactionWorker:
    def __init__(
        self,
        repository: TransactionRepository,
        processor: TransactionProcessor,
        *,
        concurrency: int,
        batch_size: int,
        poll_interval_seconds: float,
        lease_seconds: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        shutdown_grace_seconds: float,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._concurrency = concurrency
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._inflight: dict[asyncio.Task[None], TransactionRecord] = {}
        self._loop_task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        repository: TransactionRepository,
        processor: TransactionProcessor | None = None,
    ) -> "TransactionWorker":
        return cls(
            repository,
            processor or SimulatedExternalProcessor(settings.processing_delay_seconds),
            concurrency=settings.worker_concurrency,
            batch_size=settings.worker_batch_size,
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
            retry_base_seconds=settings.worker_retry_base_seconds,
            retry_max_seconds=settings.worker_retry_max_seconds,
            shutdown_grace_seconds=settings.worker_shutdown_grace_seconds,
        )

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    def notify(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run(), name="transaction-worker")
        logger.info(
            "Transaction worker started",
            extra={"concurrency": self._concurrency, "lease_seconds": self._lease_seconds},
        )

    async def stop(self) -> None:
        if self._loop_task is None:
            return
        self._stopping.set()
        self._wake.set()
        await asyncio.gather(self._loop_task, return_exceptions=True)
        self._loop_task = None
        await self._drain()
        logger.info("Transaction worker stopped")

    async def _run(self) -> None:
        consecutive_errors = 0
        while not self._stopping.is_set():
            self._wake.clear()
            limit = min(self._concurrency - len(self._inflight), self._batch_size)
            claimed: list[TransactionRecord] = []
            if limit > 0:
                try:
                    claimed = await self._repository.claim_due(
                        limit=limit, lease_seconds=self._lease_seconds
                    )
                except Exception:
                    consecutive_errors += 1
                    logger.exception(
                        "Failed to claim transactions",
                        extra={"consecutive_errors": consecutive_errors},
                    )
                    await self._wait_for_wake(self._claim_error_backoff(consecutive_errors))
                    continue
                consecutive_errors = 0
            for record in claimed:
                self._spawn(record)
            if limit > 0 and len(claimed) == limit:
                continue
            await self._wait_for_wake(self._poll_interval_seconds)

    async def _wait_for_wake(self, max_wait_seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(max_wait_seconds):
                await self._wake.wait()

    def _spawn(self, record: TransactionRecord) -> None:
        task = asyncio.create_task(
            self._execute(record), name=f"process-transaction-{record.transaction_id}"
        )
        self._inflight[task] = record
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        was_saturated = len(self._inflight) >= self._concurrency
        record = self._inflight.pop(task, None)
        if was_saturated:
            self._wake.set()
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Transaction task crashed",
                exc_info=error,
                extra={"transaction_id": record.transaction_id if record else None},
            )

    async def _execute(self, record: TransactionRecord) -> None:
        context = self._log_context(record)
        logger.info("Processing transaction", extra=context)
        try:
            await self._processor.process(record)
        except Exception as error:
            await self._handle_failure(record, error)
            return

        completed = await self._persist(
            lambda: self._repository.mark_processed(record.transaction_id, record.attempts),
            "mark transaction processed",
            context,
        )
        if completed:
            logger.info("Transaction processed", extra=context)
        else:
            logger.warning("Transaction completion was not recorded", extra=context)

    async def _handle_failure(self, record: TransactionRecord, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        context = {**self._log_context(record), "error": message}
        if record.attempts >= self._max_attempts:
            await self._persist(
                lambda: self._repository.mark_failed(
                    record.transaction_id, record.attempts, message
                ),
                "mark transaction failed",
                context,
            )
            logger.error("Transaction failed permanently", extra=context)
            return

        delay = self._retry_delay(record.attempts)
        await self._persist(
            lambda: self._repository.schedule_retry(
                record.transaction_id, record.attempts, message, delay
            ),
            "schedule transaction retry",
            context,
        )
        logger.warning(
            "Transaction processing failed, retry scheduled",
            extra={**context, "retry_in_seconds": round(delay, 3)},
        )

    async def _persist(
        self,
        operation: Callable[[], Awaitable[bool]],
        description: str,
        context: dict[str, Any],
    ) -> bool:
        for delay in (*PERSIST_RETRY_DELAYS_SECONDS, None):
            try:
                return await operation()
            except Exception:
                if delay is None:
                    logger.exception(
                        "Failed to %s, lease expiry will trigger a retry",
                        description,
                        extra=context,
                    )
                    return False
                logger.warning("Failed to %s, retrying", description, exc_info=True, extra=context)
                await asyncio.sleep(delay)
        return False

    async def _drain(self) -> None:
        if not self._inflight:
            return
        tasks = list(self._inflight)
        _, pending = await asyncio.wait(tasks, timeout=self._shutdown_grace_seconds)
        if not pending:
            return
        interrupted = [self._inflight[task] for task in pending if task in self._inflight]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for record in interrupted:
            try:
                released = await self._repository.release(record.transaction_id, record.attempts)
            except Exception:
                logger.exception(
                    "Failed to release transaction lease, it will be retried after expiry",
                    extra=self._log_context(record),
                )
                continue
            if released:
                logger.info("Released in-flight transaction", extra=self._log_context(record))

    def _retry_delay(self, attempt: int) -> float:
        ceiling = min(self._retry_max_seconds, self._retry_base_seconds * 2.0 ** (attempt - 1))
        return ceiling / 2 + random.uniform(0, ceiling / 2)

    def _claim_error_backoff(self, consecutive_errors: int) -> float:
        return min(
            CLAIM_ERROR_BACKOFF_MAX_SECONDS,
            self._poll_interval_seconds * 2.0 ** min(consecutive_errors, 10),
        )

    @staticmethod
    def _log_context(record: TransactionRecord) -> dict[str, Any]:
        return {"transaction_id": record.transaction_id, "attempt": record.attempts}
