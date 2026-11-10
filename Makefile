.PHONY: setup lint fmt typecheck test up down migrate data data-crypto data-equities data-validate ci

UV := uv

setup:
	$(UV) sync --all-groups
	$(UV) run pre-commit install

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run mypy core config data strategies research cli.py

test:
	$(UV) run pytest

up:
	docker compose up -d postgres
	docker compose exec -T postgres sh -c 'until pg_isready -U kestrel; do sleep 1; done'

down:
	docker compose down

migrate:
	$(UV) run alembic upgrade head

data-crypto:
	$(UV) run kestrel data fetch-crypto

data-equities:
	$(UV) run kestrel data fetch-equities

data-validate:
	$(UV) run kestrel data validate

## Phase 0's "done when": fetch BTC/ETH from 2019 + the ten ETFs from
## 2016, write Parquet, print a validation report.
data: data-crypto data-equities data-validate

ci: lint typecheck test
