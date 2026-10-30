import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from app import worker as worker_module
from app.models import TransactionStatus
from app.repository import TransactionRecord, TransactionRepository
from app.worker import TransactionWorker
from tests.support import RecordingProcessor, wait_until


def make_record(transaction_id: str, attempts: int = 1) -> TransactionRecord:
    now = datetime.now(UTC)
    return TransactionRecord(
        transaction_id=transaction_id,
        source_account="acc_a",
        destination_account="acc_b",
        amount=Decimal("10"),
        currency="INR",
        status=TransactionStatus.PROCESSING,
        attempts=attempts,
        last_error=None,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
        processed_at=None,
    )


class StubRepository:
    def __init__(
        self,
        records: list[TransactionRecord],
        *,
        claim_failures: int = 0,
        mark_failures: int = 0,
    ) -> None:
        self.pending = list(records)
        self.claim_failures = claim_failures
        self.mark_failures = mark_failures
        self.claim_calls = 0
        self.mark_calls = 0
        self.processed: list[str] = []

    async def claim_due(self, *, limit: int, lease_seconds: float) -> list[TransactionRecord]:
        self.claim_calls += 1
        if self.claim_failures > 0:
            self.claim_failures -= 1
            raise ConnectionError("database unavailable")
        batch, self.pending = self.pending[:limit], self.pending[limit:]
        return batch

    async def mark_processed(self, transaction_id: str, attempt: int) -> bool:
        self.mark_calls += 1
        if self.mark_failures > 0:
            self.mark_failures -= 1
            raise ConnectionError("database unavailable")
        self.processed.append(transaction_id)
        return True

    async def release(self, transaction_id: str, attempt: int) -> bool:
        return True


class ConcurrencyTrackingProcessor:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.completed = 0

    async def process(self, transaction: TransactionRecord) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        self.completed += 1


def build_worker(
    repository: StubRepository,
    processor: object,
    *,
    concurrency: int = 10,
    poll_interval_seconds: float = 0.01,
) -> TransactionWorker:
    return TransactionWorker(
        cast(TransactionRepository, repository),
        cast(RecordingProcessor, processor),
        concurrency=concurrency,
        batch_size=10,
        poll_interval_seconds=poll_interval_seconds,
        lease_seconds=5,
        max_attempts=3,
        retry_base_seconds=1,
        retry_max_seconds=8,
        shutdown_grace_seconds=0,
    )


@pytest.fixture(autouse=True)
def fast_persist_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "PERSIST_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))


def test_retry_delay_grows_exponentially_with_jitter_and_cap() -> None:
    worker = build_worker(StubRepository([]), RecordingProcessor())

    for attempt in range(1, 10):
        ceiling = min(8.0, 1.0 * 2 ** (attempt - 1))
        delay = worker._retry_delay(attempt)
        assert ceiling / 2 <= delay <= ceiling


def test_claim_error_backoff_is_capped() -> None:
    worker = build_worker(StubRepository([]), RecordingProcessor(), poll_interval_seconds=1)

    assert worker._claim_error_backoff(1) == 2
    assert worker._claim_error_backoff(3) == 8
    assert worker._claim_error_backoff(50) == worker_module.CLAIM_ERROR_BACKOFF_MAX_SECONDS


async def test_worker_survives_claim_failures() -> None:
    repository = StubRepository([make_record("txn_1")], claim_failures=2)
    processor = RecordingProcessor()
    worker = build_worker(repository, processor)

    await worker.start()
    try:
        await wait_until(lambda: _completed(repository, ["txn_1"]), within=3)
    finally:
        await worker.stop()

    assert repository.claim_calls >= 3
    assert processor.calls == ["txn_1"]


async def test_completion_write_is_retried_on_transient_errors() -> None:
    repository = StubRepository([make_record("txn_1")], mark_failures=2)
    worker = build_worker(repository, RecordingProcessor())

    await worker.start()
    try:
        await wait_until(lambda: _completed(repository, ["txn_1"]), within=3)
    finally:
        await worker.stop()

    assert repository.mark_calls == 3


async def test_completion_write_gives_up_without_crashing_the_worker() -> None:
    repository = StubRepository([make_record("txn_1"), make_record("txn_2")], mark_failures=4)
    worker = build_worker(repository, RecordingProcessor(), concurrency=1)

    await worker.start()
    try:
        await wait_until(lambda: _completed(repository, ["txn_2"]), within=3)
        assert worker.is_running
    finally:
        await worker.stop()

    assert repository.processed == ["txn_2"]


async def test_worker_respects_concurrency_limit() -> None:
    repository = StubRepository([make_record(f"txn_{index}") for index in range(8)])
    processor = ConcurrencyTrackingProcessor()
    worker = build_worker(repository, processor, concurrency=3)

    await worker.start()
    try:
        await wait_until(lambda: _all_completed(processor, 8), within=5)
    finally:
        await worker.stop()

    assert processor.peak == 3


async def test_start_is_idempotent_and_stop_without_start_is_safe() -> None:
    worker = build_worker(StubRepository([]), RecordingProcessor())

    await worker.stop()
    await worker.start()
    await worker.start()
    assert worker.is_running
    await worker.stop()
    assert not worker.is_running
    assert worker.inflight_count == 0


async def _completed(repository: StubRepository, expected: list[str]) -> bool:
    return all(transaction_id in repository.processed for transaction_id in expected)


async def _all_completed(processor: ConcurrencyTrackingProcessor, total: int) -> bool:
    return processor.completed == total
