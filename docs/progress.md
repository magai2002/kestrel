# Progress

## Phase 6: Dashboard, public API, GitHub polish — built and serving on 127.0.0.1; public URL awaits DNS + your OK

### What's built

- **Public API** (`api/app.py`, FastAPI): `/api/status`, `/equity`,
  `/positions`, `/trades`, `/strategies`, `/strategies/{id}/report`,
  `/tracking`, `/stream` (SSE).
  - Only shareable numbers: equity indexed to 100, weights in %, bps. A
    test seeds known cash, quantity and fee values and asserts none of
    them appear in any response.
  - Rate-limited in-app (token bucket per client; `X-Forwarded-For` is
    trusted only from configured proxy ranges) and again in Nginx.
  - Serves the built dashboard at `/`.
- **Admin API**: a separate app and container on `127.0.0.1:8711`,
  bearer token required, never proxied
  ([ADR 0006](adr/0006-public-api-admin-boundary.md)). It's verified
  running: 401 without the token.
- **Nightly shadow backtest** (`live/shadow.py`, 00:20 UTC inside the
  engine process): replays the bars `sim` actually consumed over the
  live period and stores the live-vs-shadow curve for `/tracking`.
- **Dashboard** (`web/`, React + Vite + TS, TanStack Query, Lightweight
  Charts with TradingView attribution, dark amber + Plus Jakarta Sans):
  Overview, Trades (with slippage and shortfall), Strategies (DSR, PBO,
  OOS curve vs baseline, cost sensitivity, parameter heatmap), Live vs
  backtest, How it works. It's built inside the Docker image (no Node on
  the host) and in CI.
- **`make demo`**: committed sample (10 US ETFs, daily, 2.7 MB), no
  Postgres, no keys. It runs an event-driven backtest over ~2 years plus
  the full walk-forward report in **52 s**. `--no-db` is refused together
  with `--holdout`: an in-memory ledger would be a way around the lock.
- **README** rewritten to spec Section 14, plus a **LICENSE** (MIT) and a
  CI job for the frontend.

### Found this phase (real bugs)

1. **Buffering vs hard limits.** The first full 10-ETF event backtest
   rejected **224 of 311 orders**. Buffering leaves untraded positions
   at their drifted weights; the group/gross check, evaluated on that
   whole vector, then rejected every buy in the group (and sells too,
   which made it worse).
   - `portfolio/rebalance.py::fit_to_limits` now makes the buffered
     vector fit: trading assets absorb the excess first, and held
     positions are trimmed only when materially over (0.5 pt).
   - Exposure checks no longer apply to sells.
   - Result: 137 orders, 0 rejected. The golden hash is unchanged
     (single-asset behaviour identical). The *honest* Sharpe went from
     0.69 to 0.64: the rejections had been quietly saving costs.
2. **Event backtests were slow** (4 min 15 s for the ETF sleeve):
   two-thirds of it pandas per-call overhead on `clip`/`where`/`replace`
   inside `TrendVolTarget`. Those are now numpy, with bit-identical
   results (golden hash unchanged), and the run takes 1 min 45 s.

### Waiting on you

- **DNS + Nginx + TLS** for the subdomain (`bot.alexeymagay.com`, the
  spec's suggestion; say if you want another). The snippet and exact
  commands are in `deploy/nginx/kestrel.conf`, not applied.
- **Screenshots / GIF for the README**: there's no browser on this box.
  Once the subdomain is live they're a two-minute job from your laptop.
- **"What I learned"** in the README is yours to write.

---

## Phase 5: Observability and VPS deployment — `sim` live on the VPS; host changes await your OK

**Running now on this VPS** (`deploy/docker-compose.prod.yml`, project
`kestrel-prod`): Postgres, a one-shot `migrate`, and `engine-sim`
trading both sleeves on real prices. Metrics are on `127.0.0.1:9710`,
Postgres publishes no host port, and memory is hard-capped. Started
2026-11-20 ~14:00 UTC; its first real decisions are the 16:00 UTC
crypto bar and the 16:30 London equities close.

### What's built

- **`Dockerfile`**: pinned uv 0.13.7, `--frozen` lockfile, runs as uid
  10001. 1.06 GB; it was 2.55 GB before removing the uv cache and the
  `chown -R` layer.
- **Prod compose**:
  - `engine-sim`.
  - `engine-sandbox` + `ib-gateway` (profile `sandbox`). The image is
    pinned to `10.50.1h`, because upstream moves from IBC to
    "ibcontroller" no earlier than 2027-01-10. The gateway has **no
    host ports**; the engine reaches it on the compose network.
  - Healthchecks, restart policies and memory limits on every service.
- **Metrics** (`observability/metrics.py`, spec Section 9 names). There
  are also a provisioned **Grafana dashboard**
  (`deploy/grafana/kestrel-dashboard.json`) and **alert rules**
  (`deploy/grafana/provisioning/alerting/`): engine heartbeat missing
  (fires on *no data*, so a dead engine alerts too), kill switch,
  reconciliation, and stale data per sleeve. They're Grafana-managed,
  since this box's Prometheus has no Alertmanager.
- **Backups**: `deploy/backup/kestrel-backup.sh` (pg_dump `-Fc`,
  verified with `pg_restore --list`, 14-day retention) plus a systemd
  timer. **The restore drill was run** against a scratch database: all
  tables came back.
- **`deploy/deploy.sh`** (git pull, build or pull from GHCR, migrate,
  up, smoke test) and `make deploy DEPLOY_HOST=...`. CI now builds the
  image on every push and pushes `ghcr.io/<repo>:<sha>` from `main`.
- **[docs/runbook.md](runbook.md)**: start/stop, deploy and roll back,
  emergency stop, each kill switch and alert, IB Gateway, key rotation,
  restore, memory budget.

### Found while deploying (real, not hypothetical)

1. **Permission denied on every feed write in the container**: uid 10001
   vs root-owned bind mounts. The **`error_burst` kill switch tripped
   within the first cycle**, did its job (equities halted, alert
   raised), and was reset by hand with `kestrel admin reset-kill-switch`
   once fixed. `deploy.sh` now chowns the data directories. The trap,
   running data commands on the host as root, is in the runbook.
2. **The validation job was OOM-killed inside its 512 MB container.**
   The block bootstrap materialised all 2,000 resamples at once
   (~320 MB). Now chunked: identical results, peak RSS 230 MB.
3. **This VPS has 3.7 GiB and no swap**, and VS Code server plus a
   Claude session take ~2 GiB while connected. `sim` fits comfortably;
   the IB Gateway (Java, 600–900 MB) is tight while an editor session is
   attached. See the runbook's memory budget.

### Waiting on you (I prepared these, nothing applied outside the project)

- **ntfy isn't installed on this box**, although the spec lists it as
  existing. Until `KESTREL_NTFY_URL` is set, alerts are logged, not
  pushed. That blocks this phase's "a stale feed fires an alert to my
  phone" check.
- **Prometheus scrape job**: `deploy/prometheus/kestrel-scrape.yml`
  (append and reload).
- **Grafana**: dashboard provider + JSON, alert rules (fill in the
  Prometheus datasource uid and the ntfy URL), and one child
  notification policy added by hand. The file explains why it doesn't
  provision policies: that would replace your existing routing tree.
- **Backup timer**: `deploy/backup/kestrel-backup.{service,timer}` into
  `/etc/systemd/system`.
- **Reboot test**: Docker is enabled at boot and every container is
  `unless-stopped`, so a reboot should bring the stack back. I haven't
  rebooted a box that also runs your VPN, DNS and other services.

---

## Phase 4: Paper trading (`sim` and `sandbox`) — built; `sandbox` blocked on credentials

`kestrel run --mode sim` runs both sleeves against real prices (Kraken
public OHLC for crypto, Yahoo for the UCITS twins) with our own fill
model. `kestrel run --mode sandbox` is fully built for the Binance Spot
Testnet and IBKR paper, but can't start until it has credentials: see
"what's blocked" below. The 72-hour unattended run happens on the VPS
deployment (Phase 5).

### What's built

- **One decision function for every mode**: `portfolio/rebalance.py`
  (constrain -> buffer -> quantities -> orders -> risk). The backtest
  engine was refactored onto it; the golden hash didn't move.
- **`live/`**:
  - `sleeve_runner.py`: one cycle — bar confirmation, settle, reconcile,
    mark, kill switches, decide, execute, persist.
  - `runner.py`: the scheduler. Bar close + delay, retry until the bar
    is published, catch-up cycle on start.
  - `feeds.py`: Kraken, Binance testnet, Yahoo twins, IBKR. Every bar
    consumed is persisted under its true venue.
  - `state.py`: Postgres state (orders journal, fills deduplicated on
    the venue fill id, sleeve state, snapshots, kill switches); new
    migration 0004.
  - `reconcile.py`, `schedule.py` (24/7 grid; LSE calendar with early
    closes), `build.py` (mode -> feed/broker), `gates.py` (`live`
    locked), `commands.py` (`run`, `admin`, `ibkr`).
- **Brokers** behind one `LiveBroker` protocol:
  - `ccxt_spot.py` (Binance testnet + Kraken), `ibkr.py` (`ib_async`).
  - `fake.py`: an in-memory venue with fault injection.
  - Kraken and IBKR-live exist and pass the contract suite but **cannot
    be selected**: `live` raises until Phase 7.
- **Live order path** (`execution/live_execution.py`): marketable limits
  with price protection (20 bps crypto, 30 bps equities; config refuses
  a protection outside the risk price band), timeout, cancel and
  re-quote up to twice. Each attempt has its own deterministic client
  id, sent to venues as a UUIDv5 (Binance caps ids at 36 chars), and is
  journalled *before* it's sent.
- **All six kill switches** (daily loss, drawdown, stale data,
  reconciliation, error burst, clock drift). They're persisted, cancel
  open orders, alert via ntfy (rate-limited), and stay tripped until
  `kestrel admin reset-kill-switch`. Flatten-on-trip is implemented but
  off ([ADR 0005](adr/0005-kill-switch-hold-vs-flatten.md)).
- **Admin CLI**: `kestrel admin status | pause | resume |
  reset-kill-switch | rebaseline`.

### Tests that make the claims checkable

- **Replay parity** (`tests/unit/live/test_replay_parity.py`): the live
  `sim` runner, driven bar by bar through the real store, feed and
  state machinery, reproduces the backtest engine's equity curve to
  1e-9 over 120 bars with the same number of fills. It passed first
  time, which is the payoff of the shared rebalance function.
- **Adapter contract suite** (`tests/contract/`): one suite, four
  adapters. Venue fakes return ccxt's documented unified structures,
  and real `ib_async` `Trade`/`Fill`/`Execution` objects for IBKR.
- **Chaos** (`tests/chaos/`): killed between send and ack, 5xx after
  acceptance, network drop and timeouts, restart mid-bar, feed outage.
  **Every scenario ends reconciled, no client id sent twice.** Manual
  real-venue drills: `tests/chaos/README.md`.
- **Opt-in integration** (`-m integration`): public Binance testnet,
  Kraken and Yahoo checks pass from this server. The private testnet
  and IBKR paper checks skip until credentials exist.

### Bugs and design problems found this phase

1. **A dead feed could never trip the stale-data switch.** The first
   draft returned `no_new_bar` *before* judging staleness, which is
   exactly the situation the switch exists for. Fixed, and covered by
   `test_stale_feed_trips_stale_data_even_without_new_bars`.
2. **Kraken's OHLC endpoint (720 bars, ~120 days at 4h) can't warm up a
   256-day EWMA.** `sim` would have sat flat forever. The Kraken and
   testnet listings now backfill from Binance's real history; their own
   bars win where both exist.
3. `prometheus_client.start_http_server` binds `0.0.0.0` by default. We
   always pass the address explicitly (ADR 0003).
4. `store.write_bars` overwrites a whole year's partition. Live feeds
   now go through a new atomic `upsert_bars` (temp file + rename).

### What's blocked on you

- **Binance Spot Testnet keys** (GitHub login at testnet.binance.vision)
  -> `KESTREL_BINANCE_TESTNET_API_KEY/_SECRET`.
- **IBKR paper credentials for IB Gateway** (IBC needs your username
  and password; Phase 5's compose file runs the gateway). After that:
  `kestrel ibkr contracts` to verify each UCITS twin resolves to exactly
  one contract, and **convert some paper EUR to USD**, or the cash
  reconciliation will (correctly) trip.
- An **IBKR market-data subscription for the LSE** decides whether
  `sim` can use IBKR's bars (`sim_feed: ibkr`) instead of Yahoo's.

---

## Phase 3: Validation pipeline — done (hold-out deliberately still locked)

`kestrel validate all --sleeve all` produces a full HTML + JSON report
for all five strategies on both sleeves in under a minute
(`reports/<run_id>/report.{html,json}`, plus `reports/summary.json`).
Every piece of spec Section 7 is in: trial ledger, hold-out lock,
walk-forward, DSR, PBO (CSCV), block bootstrap, 0–3× cost sensitivity,
per-year and per-regime tables, parameter heatmap with a sharp-peak
flag, before/after-buffering turnover. Method write-up for a non-quant
reader: [validation-methodology.md](validation-methodology.md).

### Scope change agreed before this phase: IBKR, UCITS twins, hybrid data

See [ADR 0004](adr/0004-ibkr-ucits-hybrid-data.md). In short: no Alpaca;
equities are **researched** on the ten US ETFs (Yahoo history, 2016+)
and **traded** as their UCITS twins on the LSE through IBKR (EU retail
can't buy US-domiciled ETFs). Distributing share classes were chosen
where one exists, because the Belgian TOB on them is 0.12% instead of
1.32%. Crypto is unchanged (Binance history and testnet, Kraken live).
`config/sleeves/*.yaml` now models each asset as a logical id with a
`research`, `live` and optional `sandbox` listing, plus per-asset costs
and per-sleeve risk limits. These were previously hardcoded in `cli.py`.

### The honest results (walk-forward out-of-sample, hold-out NOT yet evaluated)

| Strategy | Sleeve | DSR | OOS Sharpe | Baseline OOS Sharpe | PBO | OOS CAGR | OOS max DD | Turnover/yr | Cost drag/yr | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| trend_vol_target | crypto | 0.70 | 0.44 | 0.35 (buy & hold) | 0.92 | +4.8% | −24.1% | 1.1 | 1.0% | fails: DSR, PBO |
| trend_vol_target | equities | 0.87 | 0.85 | 0.75 (buy & hold) | 0.44 | +7.2% | −12.3% | 1.2 | 1.2% | fails: DSR |
| xs_momentum | crypto | 0.62 | 0.28 | 0.62 (equal weight) | 0.72 | +3.9% | −38.3% | 2.6 | 2.6% | fails: DSR, PBO, baseline |
| xs_momentum | equities | 0.57 | 0.28 | 0.46 (equal weight) | 0.60 | +2.3% | −15.0% | 5.1 | 3.4% | fails: DSR, PBO, baseline |
| mean_reversion | crypto | 0.00 | −3.25 | 0.73 (buy & hold BTC) | 0.00 | −46.4% | −90.8% | 74.6 | 54.4% | fails: everything |
| mean_reversion | equities | 0.22 | −0.19 | 0.75 (buy & hold SPY) | 0.70 | −0.5% | −3.4% | 5.9 | 1.9% | fails: DSR, PBO, baseline |
| buy_and_hold | crypto | 0.96 | 0.89 | 0.86 | — | +43.3% | −71.9% | 0.2 | 0.2% | survives |
| buy_and_hold | equities | 0.95 | 0.74 | 0.71 | — | +9.7% | −23.1% | 0.0 | 0.0% | fails: DSR (by a hair) |
| equal_weight_rebalanced | crypto | 0.95 | 0.86 | 0.82 | — | +42.5% | −77.4% | 0.0 | 0.0% | survives |
| equal_weight_rebalanced | equities | 0.94 | 0.72 | 0.71 | — | +9.3% | −23.1% | 0.0 | 0.0% | fails: DSR (by a hair) |

Each strategy's baseline is measured over *its own* OOS period, which
starts later for strategies with longer warm-up, so baseline numbers
differ between rows.

**What survives: nothing active.** Plainly:

- **`trend_vol_target` on equities is the only candidate worth
  watching.** It beat buy-and-hold out of sample (0.85 vs 0.75 Sharpe)
  with half the drawdown (−12% vs −23%), the grid sits on a plateau
  rather than a spike (no sharp peak) and PBO is 0.44, but DSR is 0.87,
  short of the 0.95 bar. Four years of OOS data can't yet distinguish
  it from a lucky draw of nine tries.
- **`trend_vol_target` on crypto** cut the drawdown from about −72%
  (buy-and-hold) to −24% and beat buy-and-hold over the same window. But
  PBO is 0.92: the parameter set that wins in training is usually *not*
  the one that wins next, so the specific speeds chosen mean little.
- **Cross-sectional momentum** lost to plain equal-weighting on both
  sleeves.
- **Mean reversion** is the cost lesson the spec wanted it to be. On
  equities it's +0.64 Sharpe at zero costs and −0.19 at real costs. On
  crypto (4h bars, Kraken's 0.8% taker fee) it turns over 75× a year
  and loses 54% a year to costs. It never goes past `sim`.
- **The surviving "strategies" are the baselines.** Buy-and-hold BTC/ETH
  over 2021–2024 was a strong period. Surviving here means "the
  statistics say this wasn't luck", not "you should do this": its OOS
  max drawdown was −72%.

### Bugs and design problems found this phase (none hypothetical)

1. **The whole `data/` Python package was never committed.**
   `.gitignore` had `/data/` to exclude the Parquet store, but the store
   lived *inside* the `data` package directory, so `data/store.py`,
   `validate.py`, `calendars.py` and every source module were ignored
   too. A fresh clone (and GitHub CI) couldn't import `data` at all.
   Fixed: market data now lives in `market_data/` (the
   `KESTREL_DATA_DIR` default) and `data/` is tracked.
2. **`TrendVolTarget` targeted the sleeve's full 15% volatility on
   *every* instrument**, so ten ETFs asked for several hundred percent
   gross exposure. Now each instrument gets an equal share (`1/N`) of the
   vol budget, with an `idm` (diversification multiplier) param left at
   a conservative 1.0. Single-instrument results, and therefore the
   golden hash, are unchanged.
3. **Risk *rejecting* over-limit targets meant over-limit strategies
   never traded at all.** A strategy asking for 130% gross got 0%, every
   bar. New `portfolio/constraints.py` clips per-instrument weights and
   scales groups and gross down *before* orders are built (in both the
   event engine and the vectorized path). Pre-trade risk checks remain
   the last-line guard and now only reject genuine anomalies. A new
   hypothesis property test checks that constrained output always passes
   the weight, group and gross checks.
4. **The Phase 1 notebook outputs were stale**: produced *before* Phase
   2's look-ahead fix, so they showed the leaky `TrendVolTarget` (gross
   Sharpe 1.09 then, 0.97 now). Re-executed, and SPY and the TOB story
   are now filled in with real data.
5. **The vectorized backtester's buffer loop was pandas-per-row**, so one
   crypto validation took ~6 minutes. Rewritten on numpy arrays with
   identical semantics (a test checks it against the old loop). Now
   ~10 s per strategy.

### Judgment calls worth flagging

- **The hold-out is still locked.** It can be evaluated exactly once per
  strategy/sleeve (`kestrel validate <s> --sleeve <sl> --holdout`).
  Several cost inputs are still unverified (below). If I spent the
  hold-out now and you then corrected an IBKR fee, every later report
  would be "contaminated" and the Phase 7 gate would refuse it forever.
  **Your call when to unlock it.**
- **Unverified cost inputs**, flagged in `config/sleeves/equities.yaml`
  and printed in every report:
  - IBKR LSE commission (tiered 0.05%, min USD 1.70, plus ~USD 0.21
    exchange/clearing). This comes from a third-party summary, because
    IBKR's own pricing page refuses automated fetches.
  - Each twin's TOB rate: accumulating funds are assumed
    Belgium-registered at 1.32%, and the IGLN ETC is assumed 0.12%.
  - LSE spreads, which are estimates.
- **DSR is applied to the stitched walk-forward OOS series** (with the
  ledger's trial count and Sharpe variance), not the in-sample best.
  That's the more conservative reading; documented in the methodology.
- **Walk-forward doesn't charge the cost of switching parameter sets** at
  a window boundary, and each segment starts already positioned. Both
  are small and documented in `research/validation.py`.
- **Yahoo data quality on the UCITS twins is mediocre**: one missing
  session (2025-10-24) on most twins, plus 2026-03-06 on EQQU and IDEM;
  many zero-volume days on the thin LSE USD lines (EQQU: 275). The US
  research ETFs are clean. This only affects the twins' warm-up history
  for the live runner; IBKR's own bars take over from Phase 4.

---

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
