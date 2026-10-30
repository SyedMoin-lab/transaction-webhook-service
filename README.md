# Transaction Webhook Service

A Python service that receives transaction webhooks from payment processors (such as Razorpay), acknowledges them immediately with `202 Accepted`, and processes them reliably in the background, with each transaction processed exactly once.

**Live API:** `https://<deployment-url>` *(added after deployment)*
**Interactive docs:** `<deployment-url>/docs`

---

## API

| Method | Path | Purpose | Success response |
|---|---|---|---|
| `GET` | `/` | Health check | `200` `{"status": "HEALTHY", "current_time": "2024-01-15T10:30:00Z"}` |
| `POST` | `/v1/webhooks/transactions` | Receive a transaction webhook | `202` `{"status": "ACCEPTED", "transaction_id": "...", "duplicate": false}` |
| `GET` | `/v1/transactions/{transaction_id}` | Retrieve transaction status | `200` `[{...transaction...}]` |

### Receive a webhook

```bash
curl -i -X POST "$BASE_URL/v1/webhooks/transactions" \
  -H "Content-Type: application/json" \
  -d '{
        "transaction_id": "txn_abc123def456",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR"
      }'
```

```
HTTP/1.1 202 Accepted
{"status":"ACCEPTED","transaction_id":"txn_abc123def456","duplicate":false}
```

Sending the same `transaction_id` again also returns `202`, with `"duplicate": true`, and does not trigger any further processing.

### Query a transaction

```bash
curl "$BASE_URL/v1/transactions/txn_abc123def456"
```

```json
[{
  "transaction_id": "txn_abc123def456",
  "source_account": "acc_user_789",
  "destination_account": "acc_merchant_456",
  "amount": 1500,
  "currency": "INR",
  "status": "PROCESSED",
  "created_at": "2024-01-15T10:30:00Z",
  "processed_at": "2024-01-15T10:30:30Z"
}]
```

`status` is `PROCESSING` until the background job completes, and `processed_at` is `null` until then. If a transaction fails every retry, its status becomes `FAILED`. An unknown `transaction_id` returns `404`.

### Error responses

| Status | When |
|---|---|
| `422` | The payload is invalid: missing fields, `amount <= 0`, a currency that is not a 3-letter code, or malformed JSON. |
| `401` | Signature verification is enabled and the signature is missing or wrong. |
| `503` | The database is unreachable. The response includes `Retry-After`, so the payment processor redelivers later. |

---

## Running locally

### Option 1: Docker Compose (only Docker required)

```bash
docker compose up --build
```

This starts PostgreSQL and the API on `http://localhost:8000`. Migrations run automatically on startup.

### Option 2: Poetry (for development)

Requirements: Python 3.12+, [Poetry](https://python-poetry.org/) 2.x, Docker (for PostgreSQL).

```bash
poetry install                # install runtime and dev dependencies
make db-up                    # start PostgreSQL on localhost:5433
cp .env.example .env          # optional: adjust configuration
make run                      # run migrations, then start the API on :8000
```

---

## Testing

### Automated test suite

```bash
make db-up        # the integration tests need PostgreSQL
make test         # unit and integration tests
make check        # lint, strict type check, and tests with coverage
```

The suite has 100+ tests and about 95% coverage. The integration tests run against a real PostgreSQL database, not mocks, because the guarantees being tested (`ON CONFLICT`, `SKIP LOCKED`, leases) only exist in PostgreSQL.

| Success criterion | Tests that prove it |
|---|---|
| **Single transaction** is processed after the delay | `test_single_transaction_is_processed_after_the_simulated_delay`, `test_transaction_is_processed_after_the_configured_delay` |
| **Duplicate prevention** | `test_concurrent_duplicate_webhooks_create_exactly_one_transaction` (30 parallel deliveries), `test_duplicate_webhooks_are_processed_exactly_once`, `test_competing_workers_process_each_transaction_exactly_once` |
| **Performance** under processing load | `test_webhook_responds_quickly_even_with_a_30_second_processing_delay`, `test_webhook_latency_stays_within_budget_under_processing_load` (300 requests, 50 concurrent, p95 < 500 ms) |
| **Reliability**: errors are handled and no transactions are lost | crash recovery after lease expiry, retries with backoff, `FAILED` after max attempts, release of in-flight work on graceful shutdown, survival across a restart, `503` when the database is down, stale workers unable to overwrite results |

### End-to-end smoke test (local or deployed)

`scripts/smoke_test.py` checks all four success criteria against any running instance, including the real 30-second delay:

```bash
poetry run python scripts/smoke_test.py --base-url http://localhost:8000
poetry run python scripts/smoke_test.py --base-url https://<deployment-url>
```

Output from a local run:

```
[PASS] Health check: HTTP 200 {'status': 'HEALTHY', 'current_time': '2026-10-30T08:10:01Z'}
[PASS] Webhook acknowledged: HTTP 202 in 11 ms {'status': 'ACCEPTED', 'transaction_id': 'txn_smoke_7bf82e07bfdb', 'duplicate': False}
[PASS] Transaction starts in PROCESSING: [{... 'status': 'PROCESSING', 'created_at': '2026-10-30T08:10:02Z', 'processed_at': None}]
[PASS] Duplicate deliveries handled gracefully: 10 deliveries, statuses=[202], newly created=1
[PASS] Latency under processing load: 50 webhooks, p50=35 ms, p95=54 ms, max=65 ms
Waiting for background processing (about 30 seconds)...
[PASS] Duplicates processed once: PROCESSED after ~30 s of polling, created_at=2026-10-30T08:10:02Z, processed_at=2026-10-30T08:10:32Z, records=1
[PASS] Single transaction: PROCESSED after ~30 s of polling, created_at=2026-10-30T08:10:02Z, processed_at=2026-10-30T08:10:32Z, records=1
ALL CHECKS PASSED
```

Against a deployed instance, the latency figures also include the network round-trip from wherever you run the script.

---

## How it works

```
Payment processor                 API (FastAPI)                         PostgreSQL
       │  POST /v1/webhooks/transactions   │                                  │
       │ ────────────────────────────────▶ │  INSERT … ON CONFLICT DO NOTHING │
       │                                   │ ───────────────────────────────▶ │  status = PROCESSING
       │ ◀──────── 202 Accepted (~10 ms) ── │                                  │
                                           │  notify worker                   │
                                    Background worker                         │
                                           │  claim due rows                  │
                                           │  FOR UPDATE SKIP LOCKED + lease  │
                                           │ ───────────────────────────────▶ │  attempts += 1
                                           │  simulated external call (30 s)  │
                                           │  UPDATE … WHERE attempts = n     │
                                           │ ───────────────────────────────▶ │  status = PROCESSED
```

1. **Acknowledge only what is stored.** The webhook handler validates the payload and writes it with one autocommit statement, `INSERT … ON CONFLICT (transaction_id) DO NOTHING`. It returns `202` only after that write succeeds, and processing never happens on the request path, so the response time does not depend on the 30-second job.
2. **Idempotency is enforced by the database.** `transaction_id` is the primary key, so any number of concurrent duplicate deliveries collapse into a single row with no race window. Duplicates get `202` with `"duplicate": true`, because some payment processors keep retrying until they see a success response. The first delivery's data is kept.
3. **Workers claim jobs with a lease.** The worker claims due rows with `FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the same row. Each claim sets a lease (`next_attempt_at = now() + lease`) and increments `attempts`. If a worker crashes, its lease expires and another worker picks the row up. Nothing is lost.
4. **Stale workers cannot overwrite results.** Completion is `UPDATE … WHERE status = 'PROCESSING' AND attempts = <claimed attempt>`. A worker whose lease expired and whose job was claimed again cannot write a second result, so each transaction gets exactly one `PROCESSED` transition.
5. **Retries use backoff, then a dead-letter state.** Failures are retried with exponential backoff and jitter. After `WORKER_MAX_ATTEMPTS` the status becomes `FAILED`, and the error is kept in `last_error` for inspection.
6. **Graceful shutdown.** On `SIGTERM` the worker stops claiming work, gives in-flight jobs a grace period, then releases any unfinished leases so another instance resumes them immediately.

## Technical choices

| Choice | Why |
|---|---|
| **FastAPI + Uvicorn** (uvloop, httptools) | Async I/O suits a service that mostly waits on the database and on simulated external calls. Typed request validation and OpenAPI docs come for free. |
| **PostgreSQL as both store and queue** | The webhook is durably stored and enqueued in the same atomic write. Using Celery with Redis or RabbitMQ would add a second system and a dual-write problem: the message could be enqueued but not stored, or the reverse. `SKIP LOCKED` queues are a proven pattern that handles far more than this workload needs. |
| **Neon (serverless PostgreSQL)** as the cloud database | Managed PostgreSQL with a free tier. Any PostgreSQL works: the service accepts standard `postgres://` URLs, including `sslmode` and `channel_binding` parameters, and supports PgBouncer-style poolers (`DATABASE_PGBOUNCER_MODE=true`). |
| **SQLAlchemy 2 (async) + asyncpg** | Fast, non-blocking database access. Every repository operation is a single statement in autocommit mode, so the webhook path takes one database round-trip. |
| **Alembic migrations** | Versioned, reversible schema changes. A PostgreSQL advisory lock makes concurrent deploys safe. The test suite runs upgrade, then downgrade, then upgrade. |
| **Embedded worker, optional standalone worker** | By default the worker runs inside the API process, so the whole service deploys as one unit. To scale API and processing separately, set `WORKER_ENABLED=false` on the API and run `python -m app.worker` as separate processes. Any number of either is safe. |
| **`503` + `Retry-After` when the database is down** | The service never acknowledges a webhook it could not persist. Payment processors such as Razorpay redeliver on non-2xx responses, so the transaction is delivered again rather than lost. |
| **Optional HMAC signature verification** | When `WEBHOOK_SECRET` is set, requests must carry a hex HMAC-SHA256 of the raw body. Set `WEBHOOK_SIGNATURE_HEADER=X-Razorpay-Signature` for Razorpay. It is off by default so the assessment's plain payloads work unchanged. |
| **Operational basics** | Structured JSON logs, `X-Request-ID` propagation, `X-Response-Time-Ms`, a health check that verifies the database, and a non-root multi-stage Docker image. |
| **Tooling** | Poetry for dependencies, Ruff for linting and formatting, mypy in strict mode, pytest with coverage, and GitHub Actions CI (lint, type check, tests against PostgreSQL, Docker build). |

### Interpreting the specification

- The query endpoint returns a **JSON array**, matching the specified response shape.
- Timestamps are UTC ISO-8601 with a `Z` suffix at second precision, for example `2024-01-15T10:30:00Z`. The database stores full precision.
- `amount` is stored as `NUMERIC(20, 4)` to avoid floating-point rounding, and returned as a JSON number (`1500`, `150.5`).
- Currency codes are normalized to upper case (`inr` becomes `INR`).
- The 30-second delay starts when a worker picks up the transaction. Because the worker is woken immediately on insert, `processed_at` is about 30 seconds after `created_at`.

---

## Configuration

All settings are environment variables. They can also be read from `.env` and `.env.local` (where `neon deploy` writes its variables); `.env.local` takes precedence. `.env.example` lists the common ones.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | local Compose database | PostgreSQL URL. `postgres://`, `postgresql://` and `postgresql+asyncpg://` are all accepted. |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | HTTP bind address |
| `PROCESSING_DELAY_SECONDS` | `30` | Simulated external API delay |
| `WORKER_ENABLED` | `true` | Run the background worker inside the API process |
| `WORKER_CONCURRENCY` | `100` | Maximum transactions processed in parallel per process |
| `WORKER_LEASE_SECONDS` | `120` | Claim lease. Must exceed the processing delay. |
| `WORKER_MAX_ATTEMPTS` | `5` | Attempts before a transaction is marked `FAILED` |
| `WORKER_POLL_INTERVAL_SECONDS` | `1` | Idle polling interval. New webhooks wake the worker immediately. |
| `WEBHOOK_SECRET` | *(unset)* | Turns on HMAC-SHA256 signature verification |
| `WEBHOOK_SIGNATURE_HEADER` | `X-Webhook-Signature` | Header that carries the signature |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` | `20` / `10` | Connection pool sizing |
| `DATABASE_PGBOUNCER_MODE` | `false` | Turns off prepared-statement caching, for transaction-mode poolers |
| `RUN_MIGRATIONS` | `true` | Run `alembic upgrade head` when the container starts |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | Logging |

---

## Deployment

The image is a standard Docker container that listens on `$PORT`, so it runs on Render, Railway, Fly.io, Cloud Run or Kubernetes. The repository includes a Render blueprint (`render.yaml`).

1. **Database:** create a Neon project in the same region as the service. The blueprint uses Render's `virginia` region, which pairs with Neon's AWS `us-east-1`. Copy the project's connection string.
2. **Service:** in Render, choose *New → Blueprint*, select this repository, and set `DATABASE_URL` when prompted.
3. Render builds the Dockerfile and runs migrations on startup. It uses `GET /` as the health check.
4. Verify the deployment: `poetry run python scripts/smoke_test.py --base-url https://<service>.onrender.com`.

> **Use an always-on instance.** Free tiers on Render and Koyeb suspend idle services, and the next request then waits tens of seconds for a cold start, which breaks the 500 ms requirement. The background worker also stops while the service is suspended. No data is lost (pending transactions resume on wake-up), but the processing delay is no longer about 30 seconds. The blueprint therefore uses Render's `starter` plan.

---

## Project structure

```
app/
  main.py               application factory and lifespan (engine, worker)
  __main__.py           production server entry point (python -m app)
  config.py             typed settings from environment variables
  db.py                 engine creation and connection URL normalization
  models.py             SQLAlchemy model and status enum
  schemas.py            request and response models and serialization
  repository.py         idempotent insert, lease claims, fenced state transitions
  worker.py             background worker (embedded or python -m app.worker)
  processor.py          simulated external call (30 s)
  logging_config.py     JSON and text log formatters
  api/
    routes/             health, webhooks and transactions endpoints
    dependencies.py     service container and signature verification
    middleware.py       request ID and timing middleware
    errors.py           503 and 500 exception handlers
migrations/             Alembic migrations
tests/
  unit/                 config, schemas, logging and worker logic
  integration/          API, processing, reliability, performance, entry points
scripts/smoke_test.py   end-to-end verification against any URL
```

## Known trade-offs

- **Processing is at-least-once at the external-call level.** If a worker crashes after the external call but before recording completion, the call is repeated on retry. Stored results stay exactly-once because of the attempt fencing. A real integration would pass `transaction_id` as the idempotency key to the downstream API.
- **Cross-instance wake-ups use polling.** New work wakes the worker in the same process immediately; other instances find it on their next poll (1 s by default). PostgreSQL `LISTEN/NOTIFY` could remove that delay if needed.
