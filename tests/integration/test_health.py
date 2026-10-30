import re
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from tests.support import UNREACHABLE_DATABASE_URL, build_settings, running_app

pytestmark = pytest.mark.integration

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.mark.usefixtures("engine")
async def test_health_check_reports_healthy(settings: Settings) -> None:
    async with running_app(settings) as (_, client):
        response = await client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "current_time"}
    assert body["status"] == "HEALTHY"
    assert TIMESTAMP_PATTERN.fullmatch(body["current_time"])
    reported = datetime.strptime(body["current_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - reported) < timedelta(seconds=5)


@pytest.mark.usefixtures("engine")
async def test_health_check_supports_head_requests(settings: Settings) -> None:
    async with running_app(settings) as (_, client):
        response = await client.head("/")

    assert response.status_code == 200
    assert response.content == b""


async def test_health_check_reports_unhealthy_when_database_is_unreachable() -> None:
    settings = build_settings(
        database_url=UNREACHABLE_DATABASE_URL,
        worker_enabled=False,
        health_check_timeout_seconds=1,
        database_connect_timeout_seconds=1,
    )
    async with running_app(settings) as (_, client):
        response = await client.get("/")

    assert response.status_code == 503
    assert response.json()["status"] == "UNHEALTHY"


@pytest.mark.usefixtures("engine")
async def test_request_id_is_propagated_or_generated(settings: Settings) -> None:
    async with running_app(settings) as (_, client):
        provided = await client.get("/", headers={"X-Request-ID": "req-123"})
        rejected = await client.get("/", headers={"X-Request-ID": "bad id\nwith newline"})

    assert provided.headers["X-Request-ID"] == "req-123"
    assert re.fullmatch(r"[0-9a-f]{32}", rejected.headers["X-Request-ID"])
    assert float(provided.headers["X-Response-Time-Ms"]) >= 0


@pytest.mark.usefixtures("engine")
async def test_openapi_schema_documents_all_endpoints(settings: Settings) -> None:
    async with running_app(settings) as (_, client):
        response = await client.get("/openapi.json")

    paths = response.json()["paths"]
    assert "/" in paths
    assert "post" in paths["/v1/webhooks/transactions"]
    assert "get" in paths["/v1/transactions/{transaction_id}"]


@pytest.mark.usefixtures("engine")
async def test_docs_can_be_disabled() -> None:
    async with running_app(build_settings(docs_enabled=False)) as (_, client):
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
