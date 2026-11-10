# Progress

## Phase 2: Event-driven engine, costs, sizing, risk checks — done

Built straight after Phase 1 in the same session, per your go-ahead.
`kestrel backtest trend_vol_target --sleeve crypto` runs end to end on
the real BTC/ETH history and writes the event log to Postgres —
Phase 2's literal "done when." All four Section 11 testing layers this
phase calls for pass: property (no-look-ahead, order-manager
convergence, risk limits never exceeded), parity (event engine vs.
vectorized backtester), golden (frozen fixture, stored hash), and unit.

### What's built

- **`core/`** additions: `Order`/`Fill`/`Position` (immutable value
  objects; a status change is `dataclasses.replace`, not a mutation —
  matches the event log's append-only philosophy), `Clock`
  (`SimClock`/`WallClock`), `Event`/`EventType`/`write_events`
  (`event_log.py` — deliberately placed in `core/`, not `live/` where
  the spec's own repo layout puts an `event_log.py`, because the
  backtest engine needs to write events before `live/` exists),
  `reproducibility.py` (git SHA, config hash, data hash — design rule 8).
- **`data/sources/binance_exchange_info.py`**: real tick size, step
  size and minimum notional from Binance's public `exchangeInfo`
  (BTCUSDT: tick 0.01, step 0.00001, min notional $5; ETHUSDT: tick
  0.01, step 0.0001, min notional $5 — confirmed by a live call, not
  guessed). `kestrel data sync-instruments` upserts these into the
  `instruments` table Phase 0 left nullable.
- **`portfolio/`**: `sizing.py` (`vol_target_weight` — refactored out
  of `TrendVolTarget` so the event engine's per-bar sizing and the
  vectorized strategy share one formula, not two; dual-mode on a
  scalar or a `pd.Series`), `construction.py` (weight → `Decimal`
  quantity, rounded down to step size — this is the exact line in the
  codebase where float signal maths becomes Decimal money, design rule 4).
- **`risk/`**: `limits.py` (config), `pre_trade.py` (weight cap, group
  cap, gross exposure, max order notional, price band, lot/min-notional
  — first breach wins), `kill_switch.py` (daily loss, drawdown; the
  other four kill switches in spec Section 6.3 — stale data,
  reconciliation, error burst, clock drift — are live-venue concepts
  with no venue yet, Phase 4).
- **`execution/`**: `costs.py` and `fills.py` (market fills wired into
  the order manager; passive-limit fills built and tested but not
  wired in — Phase 4's price-protected live path is what needs them),
  `brokers/sim.py`, `order_manager.py` (diff-based; a partial fill just
  shrinks next bar's proposed gap, no special-casing needed).
- **`backtest/engine.py`**: the full event-driven loop. Fills lag
  decisions by exactly one bar by construction (see `CLAUDE.md`'s
  Conventions for the precise mechanics) — the easiest place to
  silently reintroduce look-ahead, so it's structural, not a runtime check.

### Two real bugs this phase's testing caught (not hypothetical)

1. **A genuine look-ahead leak**, caught by the no-look-ahead property
   test, not a hand-picked example: `TrendVolTarget`'s forecast-scaling
   scalar was computed as `normalized.abs().mean()` — a **whole-series**
   mean. On a backtest, that mean includes bars that haven't happened
   yet relative to any earlier bar being scored, so every early
   forecast was quietly calibrated using future data. Fixed by making
   it a rolling window the same size as the strategy's own lookback
   requirement (`required_lookback_bars`) — causal by construction, and
   it also tightened the `on_bar` vs. `compute_weights` parity tolerance
   as a side effect (both paths now agree on the same finite window,
   not an approximately-converged one).
2. **A rounding-noise bug that rejected 1731 of 1754 proposed orders**
   in the first real end-to-end run. When a bar was within the
   position-buffer zone (i.e. "don't trade"), the engine was
   reconstructing a target quantity from the current *implied weight*
   (itself derived from the held quantity) instead of just keeping the
   held quantity as-is. That round trip (quantity → weight → quantity)
   isn't exactly idempotent after Decimal rounding, so it manufactured
   a spurious few-step difference from held on nearly every bar —
   proposed as an order, rejected for being under Binance's $5 minimum
   notional, every time. Fixed by branching on
   `portfolio.sizing.should_trade` directly and keeping the exact held
   quantity when not trading. Re-running after the fix: 23 orders
   proposed, 23 filled, 0 rejected.

### The real backtest run

`kestrel backtest trend_vol_target --sleeve crypto`, full stored
BTC/ETH history (2022-07-09 → 2026-11-01, 9,455 four-hour bars):

```
CAGR:            +11.4%
Annualised vol:  31.7%
Sharpe:          0.50
Sortino:         0.70
Max drawdown:    -48.3% (2,341 bars to recover)
Turnover:        0.14%/bar
Orders:          23 (23 filled, 0 rejected)
KILL SWITCH TRIPPED: daily loss -3.5% exceeds limit 3.0%
```

**Worth flagging, not quietly working around**: the daily-loss kill
switch trips on 2023-11-16 — about 16 months into the run — on a
single day's -3.5% move, which is an unremarkable magnitude for BTC/ETH
on a 4h-bar sleeve. Per spec Section 6.3 (and Phase 2's deliberate
scope), a tripped kill switch **stays tripped until reset by hand**,
and the hand-reset admin CLI doesn't exist until later — so this one
run spends its remaining ~3 years (6,482 of 9,455 bars, 69%) riding a
frozen position, not trading. The 3% daily-loss default is the spec's
own suggested number (Section 6.3), used as-is here; this backtest is
evidence that it may be tighter than a real crypto sleeve can run with
for long unattended stretches without Phase 4's reset mechanism in
place. Not changed unilaterally — loosening a safety default to make a
demo run longer is exactly the kind of judgment call this project's
own rules want flagged to you, not made silently. Worth deciding
once Phase 4 (or Phase 3's validation sweep) is in view.

### Judgment calls worth flagging

- **Kraken's real current entry-tier fee is 0.40% maker / 0.80%
  taker**, verified live — double the spec's assumed 0.25%/0.40%.
  `execution/costs.py` uses the verified number. See `docs/api-notes.md`.
- Only market/marketable orders are wired into the order manager;
  passive limits exist in `execution/fills.py` (tested) but aren't used
  until Phase 4's price-protected live path needs them.
- Kill-switch state and reconciliation are in-memory for this backtest
  path, not persisted to the `kill_switch_state` table — Postgres
  persistence matters once a live runner needs to recover state across
  a restart (Phase 4), not for a single backtest process.
- Every instrument in one `run_backtest` call must share an identical
  bar grid (same `open_time` values) — a deliberate Phase 2 scope
  limit; nothing resamples or aligns mismatched calendars yet.

---

## Phase 1: Research toolkit and first strategies — done

Built without waiting on Alpaca keys, per your go-ahead; the one piece
that genuinely needs equities data (SPY/TOB sections of the notebook)
is written to detect its absence and degrade gracefully rather than
being skipped — re-run the notebook after `make data-equities` and
those sections fill in on their own, no edits needed.

### What's built

- **`research/stats.py`**: every basic metric from spec Section 7 step
  4 except DSR/PBO (Phase 3, needs the trial ledger): CAGR, annualised
  vol, Sharpe, Sortino, Calmar, max drawdown (+ duration, worked out
  positionally via numpy — see the mypy note in the function docstring),
  turnover, exposure, hit rate, avg win/loss, cost drag, break-even
  cost. Every function takes `periods_per_year` explicitly rather than
  assuming one — see "the annualisation trap" in the notebook for why.
  24 unit tests, each against a value worked out by hand.
  - **Found and fixed while testing**: `turnover()`'s first bar-to-bar
    diff is undefined (no prior bar), but pandas' `DataFrame.sum(axis=1,
    skipna=True)` silently turns an all-NaN row into `0.0` rather than
    `NaN` — so without an explicit fix, the undefined first transition
    was counting as a real zero-turnover observation and biasing the
    average down. Fixed by dropping that row explicitly before
    averaging.
- **`research/vectorized.py`**: the quick backtester (weights → returns,
  no orders/broker). No-look-ahead by construction (target weights
  shifted one bar); a trade's cost is charged on the same bar as the
  return it enables, not the bar before — worth getting right, since
  getting it wrong would make net returns look wrong relative to gross
  in a way that's easy to not notice. **Documented limitation**: it
  measures turnover as raw weight-change, which mischarges a
  passively-drifting weight (`BuyAndHold`) as if it were trading every
  bar. The real system doesn't have this problem (the portfolio layer
  trades a *quantity* difference, not a weight difference) — see the
  module docstring and `strategies/baselines.py`'s.
- **`strategies/`**: all five from Section 5, each with a pydantic
  params schema, a vectorized `compute_weights` function, and a
  `Strategy`-protocol `on_bar` class built in the same pass (not
  deferred to Phase 2) — see `strategies/base.py` for why that's safe:
  most strategies' `on_bar` replays `compute_weights` over a bounded
  trailing window and returns the last row (their signals have no
  long-range memory beyond that window); `BuyAndHold` and
  `ShortTermMeanReversion` instead carry real incremental state (entry
  price; in-trade flag) because truncating their history *would* change
  the answer.
  - `TrendVolTarget`: Carver-style EWMA crossover + vol targeting.
    **The forecast-scaling constant is self-calibrated from the data
    being backtested, not Carver's published book constants** — we have
    no verified source for those specific numbers in this environment,
    and guessing them would violate the same "never guess" principle
    that applies to external APIs. Documented in the module docstring.
  - `CrossSectionalMomentum`: monthly rebalance, dual momentum, survivors
    inverse-vol-weighted; unqualified "slots" sit in cash rather than
    being redistributed — a documented interpretation of an
    underspecified part of the spec.
  - `ShortTermMeanReversion`: Wilder RSI(2), entry/exit thresholds.
    "Exit on recovery" is similarly underspecified; we define it as RSI
    crossing back above 50, flagged as a real interpretive choice, not
    a silently-picked default.
  - `baselines.py`: `BuyAndHold` (drifting weight, so it reads as
    correctly near-zero-turnover once Phase 2's quantity-based order
    manager exists) and `EqualWeightRebalanced`.
  - 39 strategy unit tests, mostly on synthetic trend-up/trend-down/chop
    series per spec Section 11, plus hand-computed cases for the pure
    helpers (RSI, rebalance-date arithmetic, lookback returns).
- **`notebooks/01_first_look.ipynb`**: executed top to bottom, outputs
  committed. BTC buy-and-hold (2019–2026): CAGR +47.4%, annualised vol
  61.2%, Sharpe 0.94, max drawdown -76.6% (846 days to recover — the
  COVID crash). The annualisation trap shown numerically: the same BTC
  return series reports Sharpe 0.79 at `periods_per_year=252` (the
  equities convention) vs the correct 0.94 at 365. `TrendVolTarget` on
  BTC: ~1.2%/day turnover, ~2.4%/year cost drag and a 3.47% break-even
  cost at an illustrative 50bps round-trip, gross Sharpe 1.09 → net
  0.93. SPY buy-and-hold and the TOB-on-`ShortTermMeanReversion` story:
  pending your Alpaca keys, as above.

### Judgment calls worth flagging

- `research/vectorized.py`'s cost model works in weight-space, not
  quantity-space (see "documented limitation" above) — a known,
  accepted simplification for a tool whose job is to be fast and
  approximate, not a bug to chase down.
- Strategy `on_bar` implementations recompute their signal over a
  bounded trailing window every bar rather than maintaining true
  incremental state (except `BuyAndHold`/`ShortTermMeanReversion`,
  which need real state). Simpler and automatically kept in sync with
  the vectorized version, at the cost of being O(window) per bar
  instead of O(1) — fine for 4h/1d bar-based decisions (spec's own
  non-goal: no high-frequency strategies), flagged in case Phase 2's
  full historical backtest runtime becomes annoying.

---

## Phase 0: Scaffold and data layer — mostly done, one piece blocked on you

### What's built

- **Project scaffold**: `uv` project (`pyproject.toml`), `ruff`
  (lint+format), `mypy` (strict on `core/`, ready for `execution/` and
  `risk/` once they exist), `pytest` + `pytest-cov`, `pre-commit`,
  `Makefile`, GitHub Actions CI (`.github/workflows/ci.yml`), `CLAUDE.md`.
- **`Settings`** (`config/settings.py`): pydantic-settings, env-prefixed
  `KESTREL_`, constructed once per entrypoint (CLI commands, Alembic's
  `env.py`) — never at module import time. Tests confirm this (
  `tests/unit/config/test_settings.py`).
- **Per-sleeve YAML configs** (`config/sleeves/{crypto,equities}.yaml`)
  matching Section 3.1 exactly: crypto (BTC/ETH, 4h, $10k) and equities
  (the ten ETFs, daily, $10k).
- **Core domain types** (`core/`): `Instrument`, `Bar`, `Venue`,
  `AssetClass`, `Timeframe` — enough for the data layer. `Order`,
  `Fill`, `Position`, the event types and the `Clock` protocol are
  Phase 2 work, not built yet (nothing would exercise them).
- **Data sources** (`data/sources/`): `binance_bulk.py` (monthly
  kline backfill with checksum verification and the timestamp-unit
  auto-detection described below), `ccxt_ohlcv.py` (Kraken, public,
  drops the unclosed final candle), `alpaca_bars.py` (adjusted daily
  ETF bars, `delayed_sip` feed).
- **Storage** (`data/store.py`): Parquet partitioned
  `venue/symbol/timeframe/year`, read via DuckDB. See
  [ADR 0002](adr/0002-parquet-duckdb-market-data.md) for why this
  isn't the existing InfluxDB.
- **Validation** (`data/validate.py`): gaps, duplicates, zero-volume
  bars, outlier returns (rolling robust z-score), bar-grid alignment,
  incomplete-last-bar detection, plus an NYSE-calendar-aware variant for
  equities.
- **Local Postgres** (`docker-compose.yml`) + **Alembic baseline**
  (`migrations/versions/0001_initial_instruments_table.py`, generated
  via `alembic revision --autogenerate` against a real running
  Postgres, not hand-written): one table, `instruments`. Verified
  `upgrade head` / `downgrade base` round-trips cleanly.
- **Docker installed on the VPS** for the first time this project —
  see [ADR 0003](adr/0003-docker-networking-on-shared-vps.md) for the
  UFW-safety reasoning (every container port binds to `127.0.0.1`
  only, confirmed with `ss -tlnp` after bringing Postgres up).
- **62 → 69 unit tests, 92% line coverage** on `core/config/data`
  (`core/` itself at 100%; `core/orm.py`'s 0% is expected — it's a pure
  SQLAlchemy schema, exercised by the real Alembic migration above, not
  by unit tests). `mypy --strict` clean on `core/`.

### `make data` — what actually ran

**Crypto (done, real data, no credentials needed):**

```
Binance: BTCUSDT 1h 2019-01-01..2026-11-09 → 68,605 bars (+ resampled 4h, 1d)
Binance: ETHUSDT 1h 2019-01-01..2026-11-09 → 68,605 bars (+ resampled 4h, 1d)
Kraken:  BTC-USD 4h (719 bars, the live/sim cross-check window)
Kraken:  ETH-USD 4h (719 bars)
```

**Equities: blocked on you.** `kestrel data fetch-equities` needs
Alpaca API keys (free paper-account signup, not a live-trading
credential — see `.env.example` and `docs/api-notes.md`). Add
`KESTREL_ALPACA_API_KEY_ID` / `KESTREL_ALPACA_API_SECRET_KEY` to
`.env`, then run `make data-equities data-validate`. Everything else
in Phase 0 is independent of this and is done.

### Validation report: known, documented gaps (not a clean report)

`kestrel data validate` exits 1 right now. Per the spec's own
acceptance criterion ("a clean validation report **or a documented
list of known gaps**"), here's the documentation:

- **20 missing-bar gaps**, each a few hours long, on the *same* dates
  for both BTCUSDT and ETHUSDT (e.g. 2019-03-12 02:00–07:00 UTC,
  2019-05-15 03:00–12:00 UTC, ...). **Confirmed genuine**, not a
  parsing bug: downloaded the raw March-2019 monthly archive directly
  and diffed consecutive timestamps outside any of our code — the
  7-hour hole at 2019-03-12 is present in Binance's own published file.
  All 20 gaps are from 2019; none appear in later years. Likely
  exchange-side incidents/maintenance from Binance's early history.
  Not re-fetchable from this source — Binance's public dump is what it
  is. A future phase could try backfilling these specific windows from
  a different venue's history if it ever matters for a backtest window
  that lands on one.
- **4 zero-volume bars** per symbol at 1h — plausible for thin
  overnight hours in 2019; not investigated further in Phase 0.
- **Outlier-return counts are informational, not failures.** At 1h,
  ~250–260 bars (~0.4%) out of 68,605 are flagged by the rolling
  robust-z-score check even after fixing it to use a local (30-day)
  window instead of one global statistic over 7 years (see the
  `find_outlier_returns` docstring in `data/validate.py` for the full
  story — the global version flagged *hundreds more*, including
  ordinary 2019 hourly moves, because a single multi-year MAD is
  miscalibrated by construction once volatility regimes shift). The
  remaining flagged bars at 1h are mostly genuine large hourly crypto
  moves (spot-checked several: +5%, -8.4%, -9%, all plausible,
  especially in 2019's thinner, more volatile market), not errors.
  Daily-bar outliers are the most informative tier and read exactly
  like real events: the 2020-03-12 COVID crash (-50.3% BTC, -59.1% ETH
  in one day), several Feb/Apr 2019 moves, and some 2025–2026 entries.
  Treat 1h/4h outlier counts as "worth a skim," not "must be zero."
- **Equities**: nothing to validate yet (blocked above). Note for
  later: `data/calendars.py::nyse_session_dates` and
  `data/validate.py::find_missing_sessions` deliberately compare by
  calendar date, not exact timestamp, because we don't yet know
  whether Alpaca's daily-bar timestamp is session-open, session-close
  or UTC-midnight (see `docs/api-notes.md`). Worth tightening once real
  data confirms the convention.

### Judgment calls worth flagging

- **Repo layout is flat at the repo root** (`core/`, `config/`, `data/`
  etc. as top-level packages, no `src/` layout, no wrapping `kestrel/`
  package) — read literally off the spec's Section 4.2 diagram.
- **Bar prices are `float`, not `Decimal`.** Design rule 4 scopes
  `Decimal` to "orders, fills, the ledger and anything sent to a
  broker"; a bar is market data, feeds signal maths (explicitly
  float-friendly per the same rule), and `float64` is Parquet's native
  column type. Decimal rounding to tick/step size happens once a
  target quantity becomes an order (Phase 2+).
- **Crypto universe in `config/sleeves/crypto.yaml` lists each asset
  twice** — once under `venue: binance` (for 2019+ history) and once
  under `venue: kraken` (for the live/sim forward feed, using the
  project's canonical dash-spelled symbols like `BTC-USD`, not ccxt's
  `BTC/USD` — `/` can't appear in a Parquet partition directory name).
  A single cross-venue "logical instrument" identity doesn't exist yet;
  it becomes necessary once order routing needs one (Phase 2), not
  before.
- **`core/orm.py` is a separate module from `core/instrument.py`**:
  the pydantic domain type (frozen, business-logic shape) and the
  SQLAlchemy persistence model (mutable, nullable-until-populated) are
  intentionally not the same class.

### Everything else from Phase 0's spec checklist

uv/ruff/mypy/pytest/pre-commit/Makefile/CI/CLAUDE.md/Settings/local
Postgres+Alembic: done. Full data fetch+validate: crypto done,
equities blocked on your Alpaca keys as above.
