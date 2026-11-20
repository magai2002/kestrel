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

make up               # LOCAL DEV postgres only (docker-compose.yml, 127.0.0.1:5432)
make prod-up          # production stack (deploy/docker-compose.prod.yml): postgres + migrate + engine-sim
make prod-logs        # follow engine-sim's JSON logs
make backup           # pg_dump the prod DB (see docs/runbook.md for restore)
make deploy DEPLOY_HOST=user@host   # ssh + deploy/deploy.sh
make down            # docker compose down
make migrate        # alembic upgrade head

make data-crypto     # Binance 2019+ backfill (BTC/ETH) + Kraken cross-check; no keys needed
make data-equities  # Yahoo daily backfill: 10 US research ETFs + their UCITS twins; no keys needed
make data-validate  # print a validation report for everything in the store
make data             # all three, in order — Phase 0's "done when" check

make sync-instruments  # tick/step/min-notional for Binance, Kraken, Binance testnet (public, no keys)
make backtest            # kestrel backtest trend_vol_target --sleeve crypto -- Phase 2's "done when" check
make validate            # kestrel validate all --sleeve all -- Phase 3; writes reports/<run_id>/report.{html,json}

uv run kestrel run --mode sim --once          # one catch-up cycle per sleeve, real prices, then exit
uv run kestrel run --mode sim                 # the live loop (sandbox needs testnet keys + IB Gateway)
uv run kestrel admin status                   # heartbeat, last bar, positions, tripped kill switches
uv run kestrel admin reset-kill-switch --mode sim --sleeve crypto --name all
uv run kestrel ibkr contracts                 # qualify UCITS twins against a running IB Gateway
uv run pytest -m integration                  # opt-in real-network venue checks
```

Tests: `tests/unit/live/live_helpers.py` and `tests/contract/venue_fakes.py`
are imported by bare module name, because test dirs have **no
`__init__.py`** on purpose: a `tests/unit/live/__init__.py` would make
`live` resolve to the test package and shadow the real `live/` package.

**Never pass `--holdout` to `kestrel validate` casually.** Each
strategy/sleeve's hold-out may be evaluated once; every access is
recorded in Postgres (`holdout_access`) and a second one marks that
strategy's reports "contaminated" forever, which the Phase 7 live
gate refuses. Development runs go without the flag.

Single test: `uv run pytest tests/unit/data/test_store.py -v`
Single command directly: `uv run kestrel data validate`

Re-run the Phase 1 notebook (e.g. after `make data-equities` finally has
SPY data) and commit the new outputs — don't hand-edit the `.ipynb`:

```sh
make notebook   # build cells -> ruff format the .ipynb -> jupyter execute --inplace
```

(The format step matters: CI's `ruff format --check` covers notebooks,
and formatting *before* executing keeps outputs matching the source.)

## Conventions

- **Equities: research on US ETFs, trade UCITS twins via IBKR**
  ([ADR 0004](docs/adr/0004-ibkr-ucits-hybrid-data.md)). There is no
  Alpaca anywhere any more. A sleeve's assets are logical ids (`SPY`,
  `BTC`) with `research` / `live` / `sandbox` listings
  (`config/sleeve.py`); strategies, risk group caps and price panels
  are keyed by **asset id**, never by venue instrument id.
- **Market data lives in `market_data/`, not `data/`.** `data/` is the
  Python package. Until Phase 3 the Parquet store sat inside it and
  `.gitignore`'s `/data/` silently excluded the whole package from git.
- **Build sleeve-derived inputs through `backtest/inputs.py`**
  (prices aligned on one grid, instruments, cost models, strategy params
  with `bars_per_day`/`periods_per_year` filled in), so `backtest`,
  `validate` and the live shadow backtest can't drift apart.
- **The decision step lives in exactly one place:
  `portfolio/rebalance.py::plan_rebalance`**, called by the backtest
  engine *and* the live runner. Never re-implement buffering, sizing or
  risk inline in either; `tests/unit/live/test_replay_parity.py` checks
  that live `sim` reproduces the backtest exactly.
- **Live orders: journal before you send.** `execution/live_execution.py`
  writes an `intent` row (via `Journal`) before every network call and
  looks the client id up at the venue before placing or retrying. A
  `TransientBrokerError` means "unknown", never "failed". Venue client
  ids are UUIDv5 of our readable id (`execution/brokers/client_ids.py`);
  never change `KESTREL_NAMESPACE`.
- **`live` mode is locked** (`live/gates.py`) until Phase 7's gates. The
  Kraken/IBKR-live adapters must stay unconstructable outside that path.
- **Target weights go through `portfolio/constraints.py` before orders**:
  clip per instrument, scale groups and gross *down*. Pre-trade risk
  checks are the last-line guard; if they reject a weight/group/gross
  breach, something upstream skipped the constraint step.

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
- **Fills lag decisions by exactly one bar** (`backtest/engine.py`):
  each loop iteration first fills what was decided *last* iteration
  (against *this* bar's open), marks equity at this bar's close, and
  only then makes a new decision whose orders fill *next* iteration.
  A decision and the fill it causes are never in the same iteration —
  getting this wrong is the single easiest way to reintroduce
  look-ahead into an otherwise-correct engine.
- **When "don't trade" (within the buffer), keep the position's exact
  held quantity — never reconstruct a target quantity from the
  derived current weight.** Found by running the real engine on real
  BTC/ETH data (Phase 2): that round trip (quantity -> weight ->
  quantity) isn't exactly idempotent after Decimal rounding, so
  reconstructing produced a spurious few-step difference from held
  *every single bar*, proposed as an order and rejected for being
  below minimum notional, forever (1731 of 1754 proposed orders in
  that run). `backtest/engine.py` now branches on
  `portfolio.sizing.should_trade` directly instead of going through
  `buffered_target`'s returned weight for the "don't trade" case.
- **Kill switches never reset themselves.** Six of them (`risk/kill_switch.py`
  pure checks; persisted in `kill_switch_state`). Trip -> cancel open
  orders -> ntfy alert -> halted until `kestrel admin reset-kill-switch`.
  Default on trip is cancel-and-hold (ADR 0005). The backtest engine
  still only evaluates daily loss and drawdown, in memory.

## Where things live

- `core/` — domain types: `Instrument`, `Bar`, `Order`, `Fill`,
  `Position`, `Event`/`EventType` (`event_log.py`), the `Clock`
  protocol (`SimClock`, `WallClock`), `reproducibility.py` (git SHA /
  config hash / data hash). `core/orm.py` is the separate SQLAlchemy
  persistence layer.
- `config/` — `Settings`, per-sleeve YAML (`config/sleeves/*.yaml`) +
  loader/schema (`config/sleeve.py`). Strategy param schemas live with
  each strategy module instead (`strategies/*.py`), not centralized here.
- `data/sources/` — one module per venue/purpose, each returning the
  same unified bar schema (`data/__init__.py::BAR_COLUMNS`) except
  `binance_exchange_info.py` (reference data: tick/step/min-notional,
  not bars). `yahoo_chart.py` is the free equities backfill: equity
  bars' `close_time` comes from the real exchange calendar
  (`data/calendars.py::session_schedule`, early closes included), and
  the still-trading session Yahoo returns is dropped.
- `data/store.py` — Parquet (partitioned) + DuckDB reader.
- `data/validate.py` — gap/duplicate/outlier/alignment checks +
  `ValidationReport`.
- `research/` — `stats.py` (every metric, hand-tested), `vectorized.py`
  (the fast weights-to-returns backtester; see its docstring's
  documented turnover-measurement limitation; `run_with_costs` is the
  full-cost-model variant validation uses), `overfitting.py` (PSR, DSR,
  PBO/CSCV, block bootstrap), `validation.py` (the Section 7 pipeline),
  `ledger.py` (trial ledger + hold-out lock; `SqlLedger`/`InMemoryLedger`),
  `tables.py` (per-year/regime), `report_html.py`. Grids and windows:
  `config/validation.yaml`.
- `strategies/` — `base.py` (the `Strategy` protocol + convention every
  other module here follows), one module per strategy
  (`trend_vol_target.py`, `xs_momentum.py`, `mean_reversion.py`,
  `baselines.py`), `registry.py` (name -> params/compute_weights/class).
- `portfolio/` — `sizing.py` (vol targeting, shared with
  `trend_vol_target.py`; buffering), `construction.py` (weight ->
  Decimal quantity; this is where `float` signal maths becomes
  `Decimal` money, design rule 4).
- `risk/` — `limits.py` (config), `pre_trade.py` (per-order checks,
  first-breach-wins), `kill_switch.py` (daily loss, drawdown only —
  see Conventions above for why not the other four).
- `execution/` — `costs.py` (commission incl. per-order minimum, spread,
  slippage, TOB), `fills.py` (fill models for `SimBroker`),
  `order_manager.py` (diff-based, deterministic client ids),
  `live_execution.py` (marketable limit + timeout + re-quote),
  `brokers/` (`base.py` protocols, `sim.py`, `ccxt_spot.py` +
  `binance_testnet.py`/`kraken.py`, `ibkr.py`, `fake.py` fault-injecting
  venue, `client_ids.py`).
- `live/` — `sleeve_runner.py` (one cycle), `runner.py` (scheduler; the
  only real-time code), `build.py` (mode -> feed/broker), `feeds.py`,
  `state.py` (Postgres state + in-memory twin), `reconcile.py`,
  `schedule.py`, `gates.py`, `commands.py` (`run`/`admin`/`ibkr` CLI).
- `observability/` — `logging.py` (structlog JSON), `alerts.py` (ntfy,
  rate-limited), `metrics.py` (Prometheus, spec Section 9 names).
- `backtest/` — `engine.py` (the event-driven loop), `results.py`
  (`BacktestResults` + its `format_summary()`).
- `migrations/` — Alembic; `core/orm.py`'s models: `instruments`,
  `event_log` (+`mode`), Phase 3's `trial_ledger`/`holdout_access`/
  `validation_reports`/`backtest_runs`, Phase 4's `orders`/`fills`/
  `sleeve_state`/`equity_snapshots`/`positions_snapshots`/`kill_switch_state`.
- `tests/fixtures/binance/` — two real downloaded sample rows (2019 ms,
  2025 µs) used as ground truth for the timestamp-unit detection test.
- `tests/property/` — hypothesis tests for the three Section 11
  properties: no-look-ahead, order-manager convergence under random
  partial fills, risk limits never exceeded under random targets.
- `tests/unit/backtest/test_parity.py`, `test_golden.py` — the spec's
  dedicated Parity and Golden testing layers.

## Gotchas

- **Two Postgres instances on the VPS**: `kestrel-postgres-1` (local dev
  compose, `127.0.0.1:5432`; tests and host-side `uv run` use it) and
  `kestrel-prod-postgres-1` (prod stack, no host port; reach it via
  `docker compose -f deploy/docker-compose.prod.yml exec postgres psql -U kestrel`).
  `engine-sim` writes to the *prod* one. Run prod one-offs inside the stack:
  `docker compose -f deploy/docker-compose.prod.yml run --rm --no-deps engine-sim <kestrel args>`.
- **Containers run as uid 10001** and bind-mount `market_data/` and
  `reports/`. Host-side data commands run as root create files the
  engine can't overwrite (Permission denied -> `error_burst` kill
  switch). `deploy/deploy.sh` re-chowns; see docs/runbook.md.
- **The VPS has 3.7 GiB RAM and no swap.** Every container has a hard
  `mem_limit`; keep one-off jobs streaming or chunked (the validation
  bootstrap was OOM-killed at 512 MB before it was chunked).

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
- `TrendVolTarget`'s forecast-scalar calibration must stay a **rolling**
  window, never a whole-series `.mean()` — a property test
  (`tests/property/test_no_look_ahead.py`) caught exactly this as a
  real look-ahead leak during Phase 2 (a scalar fit on the full sample
  changes every earlier bar's forecast based on bars that haven't
  happened yet). If you're tempted to simplify that calculation back to
  a plain mean, don't.
- `tests/unit/backtest/test_golden.py` has a hardcoded `EXPECTED_HASH`.
  If it fails after a change you *meant* to make, run
  `uv run python tests/unit/backtest/test_golden.py` to get the new
  hash and update the constant in the same commit — don't just paste in
  whatever the test printed without checking the change was intentional.
- `kestrel backtest` needs `kestrel data sync-instruments` run at least
  once (writes to the `instruments` table) — without it, orders have no
  real step size/min-notional to round/validate against and the engine
  warns loudly per instrument instead of failing.
