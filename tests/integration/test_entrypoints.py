import asyncio
import json
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.models import TransactionStatus
from app.repository import TransactionRepository
from app.schemas import TransactionWebhook
from tests.support import TEST_DATABASE_URL, WEBHOOK_PATH, wait_until, webhook_payload

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GRACEFUL_EXIT_CODES = frozenset({0, -signal.SIGTERM})


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def service_environment(**overrides: str) -> dict[str, str]:
    environment = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "ENVIRONMENT": "test",
        "LOG_JSON": "true",
        "LOG_LEVEL": "INFO",
        "PROCESSING_DELAY_SECONDS": "0.2",
        "WORKER_POLL_INTERVAL_SECONDS": "0.05",
        "WORKER_SHUTDOWN_GRACE_SECONDS": "0",
    }
    environment.update(overrides)
    return environment


@asynccontextmanager
async def spawn(module: str, environment: dict[str, str]) -> AsyncIterator[list[str]]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []

    async def collect() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            lines.append(raw.decode().rstrip())

    collector = asyncio.create_task(collect())
    try:
        yield lines
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        return_code = await asyncio.wait_for(process.wait(), timeout=20)
        await collector
        assert return_code in GRACEFUL_EXIT_CODES, "\n".join(lines)


def log_messages(lines: list[str]) -> list[str]:
    messages = []
    for line in lines:
        try:
            messages.append(json.loads(line)["message"])
        except (json.JSONDecodeError, KeyError):
            continue
    return messages


async def test_standalone_worker_processes_transactions_and_stops_on_sigterm(
    repository: TransactionRepository,
) -> None:
    await repository.create_if_absent(
        TransactionWebhook.model_validate(webhook_payload("txn_standalone"))
    )

    async def processed() -> bool:
        record = await repository.get("txn_standalone")
        return record is not None and record.status == TransactionStatus.PROCESSED

    async with spawn("app.worker", service_environment()) as lines:
        await wait_until(processed, within=15)

    messages = log_messages(lines)
    assert "Transaction worker started" in messages
    assert "Transaction processed" in messages
    assert "Transaction worker stopped" in messages


@pytest.mark.usefixtures("engine")
async def test_http_server_entrypoint_serves_requests_and_shuts_down_cleanly() -> None:
    port = free_port()
    environment = service_environment(PORT=str(port), HOST="127.0.0.1")
    base_url = f"http://127.0.0.1:{port}"

    async with spawn("app", environment) as lines, httpx.AsyncClient(base_url=base_url) as client:

        async def server_ready() -> bool:
            try:
                return (await client.get("/")).status_code == 200
            except httpx.TransportError:
                return False

        await wait_until(server_ready, within=15, interval=0.1)
        health = await client.get("/")
        accepted = await client.post(WEBHOOK_PATH, json=webhook_payload("txn_server"))

        async def processed() -> bool:
            response = await client.get("/v1/transactions/txn_server")
            return bool(response.json()[0]["status"] == "PROCESSED")

        await wait_until(processed, within=10)

    assert health.json()["status"] == "HEALTHY"
    assert "server" not in health.headers
    assert accepted.status_code == 202
    messages = log_messages(lines)
    assert "Service started" in messages
    assert "Service stopped" in messages
