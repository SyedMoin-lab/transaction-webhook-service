import argparse
import asyncio
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

WEBHOOK_PATH = "/v1/webhooks/transactions"
STATUS_PATH = "/v1/transactions/{transaction_id}"
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LATENCY_BUDGET_MS = 500.0


@dataclass
class Report:
    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str) -> None:
        self.results.append((name, passed, detail))
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {name}: {detail}", flush=True)

    @property
    def passed(self) -> bool:
        return all(passed for _, passed, _ in self.results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end smoke test for the transaction webhook service."
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="Service base URL")
    parser.add_argument(
        "--duplicates", type=int, default=10, help="Number of duplicate deliveries to send"
    )
    parser.add_argument(
        "--load", type=int, default=50, help="Unique webhooks sent to measure latency under load"
    )
    parser.add_argument(
        "--concurrency", type=int, default=10, help="Concurrent requests for the load check"
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="Seconds to wait for processing to finish"
    )
    return parser.parse_args()


def payload(transaction_id: str, amount: float = 1500) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": amount,
        "currency": "INR",
    }


async def post_webhook(client: httpx.AsyncClient, transaction_id: str) -> tuple[int, float, Any]:
    started = time.perf_counter()
    response = await client.post(WEBHOOK_PATH, json=payload(transaction_id))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return response.status_code, elapsed_ms, response.json()


async def fetch(client: httpx.AsyncClient, transaction_id: str) -> list[dict[str, Any]]:
    response = await client.get(STATUS_PATH.format(transaction_id=transaction_id))
    response.raise_for_status()
    body: list[dict[str, Any]] = response.json()
    return body


async def wait_for_processed(
    client: httpx.AsyncClient, transaction_id: str, max_wait_seconds: float
) -> tuple[dict[str, Any] | None, float]:
    started = time.perf_counter()
    while time.perf_counter() - started < max_wait_seconds:
        body = await fetch(client, transaction_id)
        if body and body[0]["status"] == "PROCESSED":
            return body[0], time.perf_counter() - started
        await asyncio.sleep(1)
    return None, time.perf_counter() - started


async def check_health(client: httpx.AsyncClient, report: Report) -> None:
    response = await client.get("/")
    body = response.json()
    report.record(
        "Health check",
        response.status_code == 200
        and body.get("status") == "HEALTHY"
        and bool(TIMESTAMP_PATTERN.fullmatch(str(body.get("current_time")))),
        f"HTTP {response.status_code} {body}",
    )


async def check_single_transaction(
    client: httpx.AsyncClient, report: Report, transaction_id: str
) -> None:
    status_code, elapsed_ms, body = await post_webhook(client, transaction_id)
    report.record(
        "Webhook acknowledged",
        status_code == 202 and elapsed_ms < LATENCY_BUDGET_MS,
        f"HTTP {status_code} in {elapsed_ms:.0f} ms {body}",
    )
    initial = await fetch(client, transaction_id)
    report.record(
        "Transaction starts in PROCESSING",
        len(initial) == 1 and initial[0]["status"] == "PROCESSING",
        f"{initial}",
    )


async def check_duplicates(
    client: httpx.AsyncClient, report: Report, transaction_id: str, count: int
) -> None:
    results = await asyncio.gather(*(post_webhook(client, transaction_id) for _ in range(count)))
    statuses = {status for status, _, _ in results}
    created = sum(1 for _, _, body in results if body.get("duplicate") is False)
    report.record(
        "Duplicate deliveries handled gracefully",
        statuses == {202} and created == 1,
        f"{count} deliveries, statuses={sorted(statuses)}, newly created={created}",
    )


async def check_latency_under_load(
    client: httpx.AsyncClient, report: Report, total: int, concurrency: int
) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    run_id = uuid.uuid4().hex[:8]

    async def send(index: int) -> tuple[int, float, Any]:
        async with semaphore:
            return await post_webhook(client, f"txn_load_{run_id}_{index}")

    results = await asyncio.gather(*(send(index) for index in range(total)))
    latencies = sorted(elapsed for _, elapsed, _ in results)
    statuses = {status for status, _, _ in results}
    p95 = statistics.quantiles(latencies, n=20)[-1] if len(latencies) > 1 else latencies[0]
    report.record(
        "Latency under processing load",
        statuses == {202} and p95 < LATENCY_BUDGET_MS,
        (
            f"{total} webhooks, p50={statistics.median(latencies):.0f} ms, "
            f"p95={p95:.0f} ms, max={latencies[-1]:.0f} ms"
        ),
    )


async def check_processed(
    client: httpx.AsyncClient,
    report: Report,
    transaction_id: str,
    label: str,
    max_wait_seconds: float,
) -> None:
    transaction, waited = await wait_for_processed(client, transaction_id, max_wait_seconds)
    if transaction is None:
        report.record(label, False, f"not processed after {waited:.0f} s")
        return
    rows = await fetch(client, transaction_id)
    report.record(
        label,
        len(rows) == 1 and transaction["processed_at"] is not None,
        (
            f"PROCESSED after ~{waited:.0f} s of polling, created_at={transaction['created_at']}, "
            f"processed_at={transaction['processed_at']}, records={len(rows)}"
        ),
    )


async def run(args: argparse.Namespace) -> bool:
    report = Report()
    run_id = uuid.uuid4().hex[:12]
    single_id = f"txn_smoke_{run_id}"
    duplicate_id = f"txn_dup_{run_id}"
    print(f"Testing {args.base_url}", flush=True)

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        await check_health(client, report)
        await check_single_transaction(client, report, single_id)
        await check_duplicates(client, report, duplicate_id, args.duplicates)
        await check_latency_under_load(client, report, args.load, args.concurrency)
        print("Waiting for background processing (about 30 seconds)...", flush=True)
        await asyncio.gather(
            check_processed(client, report, single_id, "Single transaction", args.timeout),
            check_processed(
                client, report, duplicate_id, "Duplicates processed once", args.timeout
            ),
        )

    print("ALL CHECKS PASSED" if report.passed else "SOME CHECKS FAILED", flush=True)
    return report.passed


def main() -> None:
    sys.exit(0 if asyncio.run(run(parse_args())) else 1)


if __name__ == "__main__":
    main()
