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

make sync-instruments  # fetch real tick/step/min-notional from Binance into the instruments table
make backtest            # kestrel backtest trend_vol_target --sleeve crypto -- Phase 2's "done when" check
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
- **The risk-pre-trade / kill-switch / order-manager split only
  computes what a backtest can know.** `risk/kill_switch.py` only
  implements daily-loss and drawdown (the other four — stale data,
  reconciliation mismatch, error burst, clock drift — are live-venue
  concepts with no venue to speak of yet, Phase 4). `execution/
  order_manager.py`'s restart-recovery lookup isn't built for the same
  reason: nothing to recover from without a live broker.

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
  not bars).
- `data/store.py` — Parquet (partitioned) + DuckDB reader.
- `data/validate.py` — gap/duplicate/outlier/alignment checks +
  `ValidationReport`.
- `research/` — `stats.py` (every metric, hand-tested), `vectorized.py`
  (the fast weights-to-returns backtester; see its docstring's
  documented turnover-measurement limitation).
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
- `execution/` — `costs.py` (commission/spread/slippage/TOB — see
  `docs/api-notes.md` for the verified Kraken/Alpaca fee facts),
  `fills.py` (market + passive-limit fill models; only market is wired
  into the order manager), `brokers/sim.py` (the only `Broker` so
  far), `order_manager.py` (diff-based, idempotent client order IDs).
- `backtest/` — `engine.py` (the event-driven loop), `results.py`
  (`BacktestResults` + its `format_summary()`).
- `migrations/` — Alembic; `core/orm.py`'s models: `instruments`
  (Phase 0) and `event_log` (Phase 2) so far.
- `tests/fixtures/binance/` — two real downloaded sample rows (2019 ms,
  2025 µs) used as ground truth for the timestamp-unit detection test.
- `tests/property/` — hypothesis tests for the three Section 11
  properties: no-look-ahead, order-manager convergence under random
  partial fills, risk limits never exceeded under random targets.
- `tests/unit/backtest/test_parity.py`, `test_golden.py` — the spec's
  dedicated Parity and Golden testing layers.

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
