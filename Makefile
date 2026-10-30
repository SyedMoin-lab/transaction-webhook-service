.PHONY: install db-up db-down migrate run worker lint format typecheck test test-unit coverage check smoke docker-build docker-up docker-down

BASE_URL ?= http://localhost:8000

install:
	poetry install

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

migrate:
	poetry run alembic upgrade head

run: migrate
	poetry run python -m app

worker:
	poetry run python -m app.workers

lint:
	poetry run ruff check .
	poetry run ruff format --check .

format:
	poetry run ruff check --fix .
	poetry run ruff format .

typecheck:
	poetry run mypy app migrations tests scripts

test:
	poetry run pytest

test-unit:
	poetry run pytest tests/unit

coverage:
	poetry run pytest --cov --cov-report=term-missing --cov-report=html

check: lint typecheck coverage

smoke:
	poetry run python scripts/smoke_test.py --base-url $(BASE_URL)

docker-build:
	docker build -t transaction-webhook-service:latest .

docker-up:
	docker compose up -d --build --wait

docker-down:
	docker compose down
