# Transaction Webhook Service

Receives transaction webhooks from payment processors, acknowledges them right away with `202 Accepted`, and processes each transaction once in the background.

Live API: `https://<deployment-url>` (docs at `/docs`)

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `POST` | `/v1/webhooks/transactions` | Receive a transaction webhook (returns `202`) |
| `GET` | `/v1/transactions/{transaction_id}` | Get a transaction's status |

```bash
curl -X POST "$BASE_URL/v1/webhooks/transactions" \
  -H "Content-Type: application/json" \
  -d '{"transaction_id":"txn_abc123def456","source_account":"acc_user_789","destination_account":"acc_merchant_456","amount":1500,"currency":"INR"}'

curl "$BASE_URL/v1/transactions/txn_abc123def456"
```

A transaction is `PROCESSING` until its 30-second job finishes. It then becomes `PROCESSED`, with `processed_at` set. Sending the same `transaction_id` again still returns `202`, with `"duplicate": true`, but the transaction is not processed a second time.

## Running locally

With Docker:

```bash
docker compose up --build
```

This starts the API on http://localhost:8000 with a local Postgres. Migrations run automatically on startup.

With Poetry (Python 3.12):

```bash
poetry install
make db-up    # Postgres on localhost:5433
make run      # runs migrations, then starts the API on :8000
```

## Testing

```bash
make db-up
make test
```

The unit and integration tests run against a real Postgres database. They cover the four success criteria:

- a single transaction is processed after the delay
- concurrent duplicate deliveries produce one transaction
- the webhook stays fast under processing load
- transactions recover from failures, crashes and restarts

To test a running instance end to end, including the real 30-second delay:

```bash
poetry run python scripts/smoke_test.py --base-url http://localhost:8000
```

## Technical choices

- FastAPI with async SQLAlchemy and asyncpg, since the service spends most of its time waiting on I/O.
- Postgres is both the storage and the job queue. The webhook is saved with a single insert before the `202` is sent, so an acknowledged transaction can't be lost. A separate broker such as Celery with Redis would mean keeping two systems in sync.
- Idempotency is enforced by the database. `transaction_id` is the primary key and the insert uses `ON CONFLICT DO NOTHING`, so concurrent duplicates end up as one row.
- The background worker claims jobs with `FOR UPDATE SKIP LOCKED` and a time-limited lease. If a worker dies mid-job, the lease expires and another worker picks the job up. Results are only saved for the current attempt, so a stale worker can't record a second result.
- Failed jobs are retried with exponential backoff. After 5 attempts the status becomes `FAILED`.
- If the database is unreachable, the webhook returns `503`, so the payment processor retries instead of the transaction being dropped.
- Neon hosts the database and Render hosts the service, on an always-on instance so that the 500 ms limit holds and the worker keeps running.

The worker runs inside the API process by default. To scale them separately, set `WORKER_ENABLED=false` on the API and run `python -m app.workers` as its own process.

## Configuration

Environment variables (see `.env.example`):

- `DATABASE_URL`: Postgres connection string. Neon URLs work as copied.
- `PROCESSING_DELAY_SECONDS`: default `30`
- `WORKER_ENABLED`: default `true`
- `WEBHOOK_SECRET`: optional. When set, each request must include an HMAC-SHA256 signature in the `X-Webhook-Signature` header.

## Deployment

`render.yaml` deploys the Docker image to Render:

1. Create a Neon database in AWS us-east-1.
2. In Render, choose New → Blueprint and select this repo.
3. Set `DATABASE_URL` when prompted.
