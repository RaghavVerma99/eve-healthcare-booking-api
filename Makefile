.PHONY: help install dev-install lint format test test-postgres run migrate seed worker compose-up compose-down clean

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
RUFF := $(VENV)/bin/ruff
PYTEST := $(VENV)/bin/pytest

# Override to point the suite at a different PostgreSQL instance.
TEST_PG_URL ?= postgresql+asyncpg://eve:eve@localhost:5432/eve_diagnostics_test

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install runtime dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

dev-install: ## Install runtime + dev dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

lint: ## Lint with ruff
	$(RUFF) check app tests alembic

format: ## Auto-format and auto-fix
	$(RUFF) check --fix app tests alembic
	$(RUFF) format app tests

test: ## Run the test suite (SQLite by default)
	$(PYTEST) -q

test-postgres: ## Run the test suite against PostgreSQL (create the DB first)
	@echo "Using $(TEST_PG_URL)"
	@echo "If it is missing, create it with: createdb -U eve eve_diagnostics_test"
	TEST_DATABASE_URL="$(TEST_PG_URL)" $(PYTEST) -q

run: ## Start the dev server with reload
	$(VENV)/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate: ## Apply database migrations
	$(VENV)/bin/alembic upgrade head

seed: ## Seed centres, tests and the admin user
	$(PY) -m app.db.seed

worker: ## Start the Celery worker
	$(VENV)/bin/celery -A app.worker.celery_app worker --loglevel=info -Q payments,default

compose-up: ## Start the full stack (db, redis, migrate, seed, api, worker)
	docker compose up --build

compose-down: ## Stop the stack and remove volumes
	docker compose down -v

clean: ## Remove caches and local databases
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov dev.db test.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
