.PHONY: setup lint fmt typecheck test up down migrate data data-crypto data-equities data-validate sync-instruments backtest validate notebook deploy prod-up prod-logs backup ci

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
	$(UV) run mypy core config data strategies research portfolio risk execution backtest live observability cli.py

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

sync-instruments:
	$(UV) run kestrel data sync-instruments

## Phase 2's "done when": prints a results summary and writes the event log.
backtest:
	$(UV) run kestrel backtest trend_vol_target --sleeve crypto

## Phase 3's "done when": full walk-forward reports for every strategy on
## both sleeves (hold-out NOT evaluated; see docs/validation-methodology.md).
validate:
	$(UV) run kestrel validate all --sleeve all

## Regenerate the Phase 1 notebook: build cells -> ruff-format them (CI's
## lint checks .ipynb too) -> execute in place, so committed source and
## outputs always match. Never hand-edit the .ipynb.
notebook:
	$(UV) sync --group notebooks
	$(UV) run python notebooks/build_01_first_look.py
	$(UV) run ruff format notebooks/01_first_look.ipynb
	$(UV) run jupyter execute --inplace notebooks/01_first_look.ipynb

## Phase 5: production stack (deploy/docker-compose.prod.yml), 127.0.0.1-only ports.
PROD := docker compose -f deploy/docker-compose.prod.yml
prod-up:
	$(PROD) up -d
prod-logs:
	$(PROD) logs -f --tail 100 engine-sim
backup:
	./deploy/backup/kestrel-backup.sh

## Deploy over SSH: make deploy DEPLOY_HOST=user@host [DEPLOY_DIR=...]
DEPLOY_DIR ?= /root/trading_bot/kestrel
deploy:
	@test -n "$(DEPLOY_HOST)" || (echo "usage: make deploy DEPLOY_HOST=user@host" && exit 1)
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_DIR) && ./deploy/deploy.sh'

ci: lint typecheck test
