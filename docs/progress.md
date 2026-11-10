# Progress

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
