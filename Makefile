# Commands for Debian/Ubuntu. Run `make help` to list them.
VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help venv install dirs run run-db test test-integration lint format check \
	    db-up db-down docker-build docker-run docker-down docker-reset psql clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

venv: ## Create the virtual environment
	python3 -m venv $(VENV)

install: venv ## Install runtime and development dependencies
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

dirs: ## Create data/ and logs/ (needed before Docker bind mounts)
	mkdir -p data logs

run: ## Run the pipeline locally (files only, no database)
	$(PYTHON) -m src.main --skip-db

run-db: ## Run the pipeline locally and load PostgreSQL (needs .env and a running database)
	$(PYTHON) -m src.main --load-db

test: ## Run the unit tests (integration tests are skipped without a database)
	$(PYTHON) -m pytest

test-integration: ## Run only the PostgreSQL integration tests (needs TEST_POSTGRES_*)
	$(PYTHON) -m pytest -m integration

lint: ## Lint and check formatting with ruff
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

format: ## Auto-format and fix lint issues
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

check: lint test ## Lint then test

db-up: dirs ## Start PostgreSQL in Docker (waits until healthy)
	docker compose up -d --wait db

db-down: ## Stop containers (database volume is kept)
	docker compose down

docker-build: ## Build the pipeline image
	docker compose build app

docker-run: dirs docker-build ## Run the whole pipeline in Docker (starts PostgreSQL if needed)
	docker compose run --rm app

docker-down: ## Stop containers (database volume is kept)
	docker compose down

docker-reset: ## Stop containers AND delete the database volume
	docker compose down -v

psql: ## Open a psql shell in the database container
	docker compose exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

clean: ## Remove generated data, logs and caches (keeps data/README.md)
	find data -mindepth 2 -type f -delete 2>/dev/null || true
	rm -rf logs .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
