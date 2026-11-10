# CLAUDE.md

Build spec: [docs/SPEC.md](docs/SPEC.md) — read it before working on anything
here. Phase status: [docs/progress.md](docs/progress.md). API quirks worth
knowing before touching a venue adapter: [docs/api-notes.md](docs/api-notes.md).

## Commands

```sh
make setup          # uv sync + pre-commit install
make lint            # ruff check + ruff format --check
make fmt              # ruff format + ruff check --fix
make typecheck      # mypy (strict on core/, execution/, risk/)
make test            # pytest (excludes -m integration)
make ci               # lint + typecheck + test, what CI runs

make up               # docker compose up postgres (127.0.0.1:5432 only)
make down            # docker compose down
make migrate        # alembic upgrade head

make data-crypto     # Binance 2019+ backfill (BTC/ETH) + Kraken cross-check; no keys needed
make data-equities  # Alpaca daily ETF backfill; needs KESTREL_ALPACA_* in .env
make data-validate  # print a validation report for everything in the store
make data             # all three, in order — Phase 0's "done when" check
```

Single test: `uv run pytest tests/unit/data/test_store.py -v`
Single command directly: `uv run kestrel data validate`

Re-run the Phase 1 notebook (e.g. after `make data-equities` finally has
SPY data) and commit the new outputs — don't hand-edit the `.ipynb`:

```sh
uv sync --group notebooks   # jupyter/nbclient/matplotlib; not in the main dep list
uv run jupyter execute --inplace notebooks/01_first_look.ipynb
```

## Conventions

- **No module-level reads of `os.environ`.** One `Settings` object
  (`config/settings.py`, pydantic-settings) is constructed once per
  process entrypoint (a CLI command in `cli.py`, or Alembic's
  `migrations/env.py`) and passed down explicitly. Functions in
  `data/`, `core/` etc. take plain parameters, never import `Settings`
  themselves. (A past project got bitten by a dotenv load-order bug;
  this is the fix.)
- **Repo layout is flat at the repo root**: `core/`, `config/`,
  `data/`, `cli.py` etc. are top-level packages, not nested under
  `src/` or a wrapping `kestrel/` package. Matches SPEC.md Section 4.2
  literally.
- **All timestamps are timezone-aware UTC.** `core.bar.Bar` rejects
  naive or non-UTC datetimes at construction.
- **Money vs. market data**: `Decimal` is for orders/fills/ledger
  (design rule 4) — `core/orm.py`'s nullable `tick_size`/`step_size`/
  `min_notional` use it. `core.bar.Bar` prices are `float` (research
  and signal maths are explicitly float-friendly, and it's Parquet's
  native column type).
- **Symbols never contain `/`.** Kraken's ccxt-unified symbols
  (`BTC/USD`) are spelled `BTC-USD` everywhere outside the ccxt API
  call itself — `data/store.py`'s Hive-style Parquet partitions use the
  symbol as a directory name.
- **`core/instrument.py` vs `core/orm.py`**: the former is the frozen
  pydantic domain type business logic reasons about; the latter is the
  mutable SQLAlchemy persistence model. Deliberately not the same
  class — a migration changing storage shape shouldn't force a change
  to how strategies reason about an instrument.
- **Every Docker-published port binds to `127.0.0.1` explicitly**,
  never a bare `host:container` mapping (which Compose publishes to
  `0.0.0.0`). This VPS has a hand-tuned UFW firewall that Docker's own
  iptables rules don't consult — see
  [docs/adr/0003-docker-networking-on-shared-vps.md](docs/adr/0003-docker-networking-on-shared-vps.md).
- **Never guess at a venue's API shape.** Read current docs or, better,
  pull a real response/file and inspect it (see `docs/api-notes.md` for
  why — Binance's bulk-data timestamp unit isn't documented anywhere
  and silently changed between 2019 and 2025 files).
- **Every strategy module exposes three things that must agree**: a
  pydantic params schema, a vectorized `compute_weights(prices, params)
  -> pd.DataFrame` (Phase 1; `research/vectorized.py` and later Phase
  3's walk-forward grid search), and a `Strategy`-protocol class with
  `on_bar(ctx) -> dict[str, float]` (Phase 2's event engine). See
  `strategies/base.py` for the convention and which strategies can get
  away with `on_bar` just replaying `compute_weights` over a trailing
  window (most of them) vs. needing real incremental state
  (`BuyAndHold`, `ShortTermMeanReversion` — truncating their history
  would silently change the answer).
- **Don't hand-edit `notebooks/*.ipynb`.** Edit the generator script
  pattern (rebuild cells, re-run `jupyter execute --inplace`) so the
  committed outputs always match the committed source exactly.

## Where things live

- `core/` — domain types (`Instrument`, `Bar`, enums). `Order`, `Fill`,
  `Position`, events, the `Clock` protocol: not built yet, arrive in
  Phase 2.
- `config/` — `Settings`, per-sleeve YAML (`config/sleeves/*.yaml`) +
  loader/schema (`config/sleeve.py`). Strategy param schemas: Phase 1.
- `data/sources/` — one module per venue, each returning the same
  unified bar schema (`data/__init__.py::BAR_COLUMNS`).
- `data/store.py` — Parquet (partitioned) + DuckDB reader.
- `data/validate.py` — gap/duplicate/outlier/alignment checks +
  `ValidationReport`.
- `migrations/` — Alembic; `core/orm.py` is the only module with
  SQLAlchemy models so far (`instruments` table only — nothing else has
  a writer yet).
- `tests/fixtures/binance/` — two real downloaded sample rows (2019 ms,
  2025 µs) used as ground truth for the timestamp-unit detection test.

## Gotchas

- `uv`/`docker` were not installed on this VPS before Phase 0; both are
  now (`source ~/.local/bin/env` for `uv`'s PATH entry in a fresh
  shell). Postgres's container port is bound to `127.0.0.1:5432` only.
- DuckDB's own `fetchdf()` triggers a `Pandas4Warning` internally
  (`DataFrame.drop(inplace=True)` deprecation) — not our code; harmless
  until DuckDB updates.
- `kestrel data validate` currently exits 1 — see
  [docs/progress.md](docs/progress.md) for the documented, confirmed-genuine
  known gaps (traced to Binance's own raw archive, not our parsing) and
  why the outlier-return counts are informational rather than failures.
