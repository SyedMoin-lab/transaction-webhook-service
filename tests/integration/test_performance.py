import asyncio
import statistics
import time

import pytest
from httpx import AsyncClient

from tests.support import WEBHOOK_PATH, build_settings, running_app, webhook_payload

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("engine")]

TOTAL_REQUESTS = 300
CONCURRENCY = 50
LATENCY_BUDGET_SECONDS = 0.5


async def timed_post(client: AsyncClient, semaphore: asyncio.Semaphore, index: int) -> float:
    async with semaphore:
        started = time.perf_counter()
        response = await client.post(WEBHOOK_PATH, json=webhook_payload(f"txn_load_{index}"))
        elapsed = time.perf_counter() - started
    assert response.status_code == 202
    return elapsed


async def test_webhook_latency_stays_within_budget_under_processing_load() -> None:
    settings = build_settings(
        processing_delay_seconds=30,
        worker_lease_seconds=120,
        worker_concurrency=100,
        worker_poll_interval_seconds=0.05,
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with running_app(settings) as (app, client):
        latencies = await asyncio.gather(
            *(timed_post(client, semaphore, index) for index in range(TOTAL_REQUESTS))
        )
        worker = app.state.container.worker
        assert worker is not None
        assert worker.inflight_count > 0

    p95 = statistics.quantiles(latencies, n=20)[-1]
    assert p95 < LATENCY_BUDGET_SECONDS
    assert max(latencies) < LATENCY_BUDGET_SECONDS * 2
