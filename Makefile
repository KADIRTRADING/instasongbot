.PHONY: help venv install install-dev run run-webhook worker \
        migrate migration downgrade \
        test test-live test-cov lint lint-fix format \
        docker-build docker-up docker-up-prod docker-down docker-logs \
        dev-services dev-services-down clean

# Default target: `make` with no arguments shows this list, instead of
# silently running the first rule (which would be `help` anyway here, but
# being explicit avoids surprises as rules get added above it later).
.DEFAULT_GOAL := help

PYTHON ?= python3
VENV := .venv

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# --- Local (no Docker) setup ------------------------------------------------

venv: ## Create a local virtualenv at .venv
	$(PYTHON) -m venv $(VENV)
	@echo "Activate with: source $(VENV)/bin/activate"

install: ## Install runtime dependencies into the active environment
	pip install -r requirements.txt

install-dev: ## Install runtime + dev/test dependencies
	pip install -r requirements-dev.txt

# --- Running the bot / worker locally (no Docker) ---------------------------
# These assume Postgres/Redis are already reachable -- either via
# `make dev-services` (below) or your own local install -- and that a `.env`
# exists (see `.env.example`) with at least BOT_TOKEN and DATABASE_URL set.

run: ## Run the bot in long-polling mode (python -m app.main)
	python -m app.main

run-webhook: ## Run the bot in webhook mode (python -m app.webhook_app)
	python -m app.webhook_app

worker: ## Run one arq worker process
	arq app.workers.settings.WorkerSettings

dev-services: ## Start just Postgres+Redis in the background (for local, non-Docker bot/worker runs)
	docker compose up -d postgres redis

dev-services-down: ## Stop the Postgres+Redis containers started by dev-services
	docker compose stop postgres redis

# --- Database migrations (Alembic) ------------------------------------------

migrate: ## Apply all pending migrations (alembic upgrade head)
	alembic upgrade head

migration: ## Autogenerate a new migration from model changes -- usage: make migration name="add foo column"
	alembic revision --autogenerate -m "$(name)"

downgrade: ## Roll back the most recent migration
	alembic downgrade -1

# --- Testing -----------------------------------------------------------------

test: ## Run the fast test suite (excludes tests marked @pytest.mark.live)
	pytest

test-live: ## Run ONLY the live tests (real AudD/Pinterest/TikTok/Redis calls -- see each test file's docstring for what it needs reachable)
	pytest -m live

test-cov: ## Run the fast test suite with a coverage report
	pytest --cov=app --cov-report=term-missing

# --- Lint / format -----------------------------------------------------------

lint: ## Check code style and common bugs (ruff check)
	ruff check .

lint-fix: ## Same as lint, but auto-fix what ruff can fix safely
	ruff check --fix .

format: ## Auto-format import order (ruff's isort-equivalent rule set)
	ruff check --fix --select I .

# --- Docker ------------------------------------------------------------------

docker-build: ## Build the bot/worker image
	docker compose build

docker-up: ## Start the full dev stack (postgres, redis, bot, worker) in the foreground
	docker compose up --build

docker-up-prod: ## Start the full stack using the production overlay (detached) -- see docker-compose.prod.yml's header comment before using this
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

docker-down: ## Stop and remove all containers started by docker-up/docker-up-prod
	docker compose down

docker-logs: ## Follow logs from every service in the dev stack
	docker compose logs -f

# --- Cleanup -----------------------------------------------------------------

clean: ## Remove Python/pytest/ruff caches
	find . -type d -name '__pycache__' -not -path './.venv*' -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
