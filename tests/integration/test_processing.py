import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import TransactionStatus
from app.repositories.transactions import TransactionRecord, TransactionRepository
from app.schemas.transactions import TransactionWebhook
from app.services.processor import SimulatedExternalProcessor, TransactionProcessor
from app.workers.transaction_worker import TransactionWorker
from tests.support import (
    WEBHOOK_PATH,
    BlockingProcessor,
    FlakyProcessor,
    RecordingProcessor,
    build_settings,
    running_app,
    wait_until,
    webhook_payload,
)

pytestmark = pytest.mark.integration


def make_webhook(transaction_id: str) -> TransactionWebhook:
    return TransactionWebhook.model_validate(webhook_payload(transaction_id))


def make_worker(
    repository: TransactionRepository,
    processor: TransactionProcessor,
    **overrides: object,
) -> TransactionWorker:
    return TransactionWorker.from_settings(build_settings(**overrides), repository, processor)


async def status_of(repository: TransactionRepository, transaction_id: str) -> TransactionRecord:
    record = await repository.get(transaction_id)
    assert record is not None
    return record


def has_status(
    repository: TransactionRepository, transaction_id: str, status: TransactionStatus
) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await status_of(repository, transaction_id)).status == status

    return check


async def test_single_transaction_is_processed_after_the_simulated_delay(
    repository: TransactionRepository,
) -> None:
    delay = 0.4
    await repository.create_if_absent(make_webhook("txn_single"))
    worker = make_worker(repository, SimulatedExternalProcessor(delay))

    await worker.start()
    try:
        await wait_until(has_status(repository, "txn_single", TransactionStatus.PROCESSED))
    finally:
        await worker.stop()

    record = await status_of(repository, "txn_single")
    assert record.attempts == 1
    assert record.last_error is None
    assert record.processed_at is not None
    assert (record.processed_at - record.created_at).total_seconds() >= delay


async def test_duplicate_webhooks_are_processed_exactly_once(engine: AsyncEngine) -> None:
    processor = RecordingProcessor(delay_seconds=0.2)
    async with running_app(build_settings(), processor) as (_, client):
        responses = await asyncio.gather(
            *(client.post(WEBHOOK_PATH, json=webhook_payload("txn_dup")) for _ in range(10))
        )
        await asyncio.sleep(0.1)
        responses.extend(
            [await client.post(WEBHOOK_PATH, json=webhook_payload("txn_dup")) for _ in range(5)]
        )

        repository = TransactionRepository(engine)
        await wait_until(has_status(repository, "txn_dup", TransactionStatus.PROCESSED))
        await asyncio.sleep(0.3)

    assert all(response.status_code == 202 for response in responses)
    assert processor.calls == ["txn_dup"]


async def test_competing_workers_process_each_transaction_exactly_once(
    repository: TransactionRepository,
) -> None:
    transaction_ids = [f"txn_{index:03d}" for index in range(40)]
    for transaction_id in transaction_ids:
        await repository.create_if_absent(make_webhook(transaction_id))

    processor = RecordingProcessor(delay_seconds=0.05)
    workers = [
        make_worker(repository, processor, worker_concurrency=5, worker_batch_size=3)
        for _ in range(3)
    ]
    for worker in workers:
        await worker.start()

    async def all_processed() -> bool:
        return len(processor.calls) >= len(transaction_ids) and all(
            [
                (await status_of(repository, transaction_id)).status == TransactionStatus.PROCESSED
                for transaction_id in transaction_ids
            ]
        )

    try:
        await wait_until(all_processed, within=10)
    finally:
        for worker in workers:
            await worker.stop()

    assert sorted(processor.calls) == transaction_ids


async def test_transient_failures_are_retried_until_success(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(make_webhook("txn_flaky"))
    processor = FlakyProcessor(failures=2)
    worker = make_worker(repository, processor, worker_max_attempts=5)

    await worker.start()
    try:
        await wait_until(has_status(repository, "txn_flaky", TransactionStatus.PROCESSED))
    finally:
        await worker.stop()

    record = await status_of(repository, "txn_flaky")
    assert processor.calls == 3
    assert record.attempts == 3
    assert record.last_error is None


async def test_persistent_failures_are_marked_failed_after_max_attempts(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(make_webhook("txn_broken"))
    processor = FlakyProcessor(failures=100)
    worker = make_worker(repository, processor, worker_max_attempts=3)

    await worker.start()
    try:
        await wait_until(has_status(repository, "txn_broken", TransactionStatus.FAILED))
    finally:
        await worker.stop()

    record = await status_of(repository, "txn_broken")
    assert processor.calls == 3
    assert record.attempts == 3
    assert record.processed_at is None
    assert record.last_error == "RuntimeError: Simulated upstream failure #3"


async def test_transactions_from_a_crashed_worker_are_recovered_after_lease_expiry(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(make_webhook("txn_orphan"))
    claimed = await repository.claim_due(limit=10, lease_seconds=0.3)
    assert [record.transaction_id for record in claimed] == ["txn_orphan"]

    processor = RecordingProcessor()
    worker = make_worker(repository, processor)
    await worker.start()
    try:
        await asyncio.sleep(0.15)
        assert processor.calls == []
        await wait_until(has_status(repository, "txn_orphan", TransactionStatus.PROCESSED))
    finally:
        await worker.stop()

    record = await status_of(repository, "txn_orphan")
    assert processor.calls == ["txn_orphan"]
    assert record.attempts == 2


async def test_stale_lease_holders_cannot_complete_a_reclaimed_transaction(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(make_webhook("txn_fenced"))
    first = (await repository.claim_due(limit=1, lease_seconds=0.1))[0]
    await asyncio.sleep(0.2)
    second = (await repository.claim_due(limit=1, lease_seconds=60))[0]

    assert (first.attempts, second.attempts) == (1, 2)
    assert await repository.mark_processed("txn_fenced", first.attempts) is False
    assert await repository.release("txn_fenced", first.attempts) is False
    assert await repository.mark_processed("txn_fenced", second.attempts) is True
    assert await repository.mark_processed("txn_fenced", second.attempts) is False
    assert (await status_of(repository, "txn_fenced")).status == TransactionStatus.PROCESSED


async def test_claim_respects_limit_order_and_active_leases(
    repository: TransactionRepository,
) -> None:
    for index in range(5):
        await repository.create_if_absent(make_webhook(f"txn_order_{index}"))

    first_batch = await repository.claim_due(limit=2, lease_seconds=60)
    second_batch = await repository.claim_due(limit=10, lease_seconds=60)
    third_batch = await repository.claim_due(limit=10, lease_seconds=60)

    assert [record.transaction_id for record in first_batch] == ["txn_order_0", "txn_order_1"]
    assert [record.transaction_id for record in second_batch] == [
        "txn_order_2",
        "txn_order_3",
        "txn_order_4",
    ]
    assert third_batch == []
    assert all(record.next_attempt_at > datetime.now(UTC) for record in first_batch)


async def test_graceful_shutdown_releases_in_flight_transactions(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(make_webhook("txn_inflight"))
    blocking = BlockingProcessor()
    worker = make_worker(repository, blocking, worker_shutdown_grace_seconds=0.1)

    await worker.start()
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    await worker.stop()

    released = await status_of(repository, "txn_inflight")
    assert released.status == TransactionStatus.PROCESSING
    assert released.attempts == 0
    assert released.next_attempt_at <= datetime.now(UTC)

    processor = RecordingProcessor()
    successor = make_worker(repository, processor)
    await successor.start()
    try:
        await wait_until(has_status(repository, "txn_inflight", TransactionStatus.PROCESSED))
    finally:
        await successor.stop()
    assert processor.calls == ["txn_inflight"]


async def test_graceful_shutdown_waits_for_jobs_that_finish_within_grace_period(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(make_webhook("txn_grace"))
    processor = RecordingProcessor(delay_seconds=0.2)
    worker = make_worker(repository, processor, worker_shutdown_grace_seconds=2)

    await worker.start()
    await wait_until(lambda: _has_calls(processor))
    await worker.stop()

    record = await status_of(repository, "txn_grace")
    assert record.status == TransactionStatus.PROCESSED
    assert record.attempts == 1


async def test_notify_wakes_an_idle_worker_immediately(repository: TransactionRepository) -> None:
    processor = RecordingProcessor()
    worker = make_worker(repository, processor, worker_poll_interval_seconds=30)
    await worker.start()
    try:
        await asyncio.sleep(0.1)
        await repository.create_if_absent(make_webhook("txn_wake"))
        worker.notify()
        await wait_until(has_status(repository, "txn_wake", TransactionStatus.PROCESSED), within=2)
    finally:
        await worker.stop()


async def test_accepted_transactions_survive_a_service_restart(
    repository: TransactionRepository,
) -> None:
    async with running_app(build_settings(worker_enabled=False)) as (_, client):
        response = await client.post(WEBHOOK_PATH, json=webhook_payload("txn_restart"))
        assert response.status_code == 202

    assert (await status_of(repository, "txn_restart")).status == TransactionStatus.PROCESSING

    processor = RecordingProcessor()
    async with running_app(build_settings(), processor):
        await wait_until(has_status(repository, "txn_restart", TransactionStatus.PROCESSED))

    assert processor.calls == ["txn_restart"]


async def _has_calls(processor: RecordingProcessor) -> bool:
    return bool(processor.calls)
