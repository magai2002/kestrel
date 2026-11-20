# API notes

Things about the three venues' APIs that surprised us, verified against
real responses/current source rather than trusted from memory or a
README (SPEC.md Section 0, rule 3: "never guess at external APIs").

## Binance public bulk data (`data.binance.vision`)

- **Timestamp unit silently changes.** Downloaded and compared two real
  monthly kline files directly:
  - `BTCUSDT-1h-2019-09.zip`: `open_time` = `1567296000000` (13 digits,
    **milliseconds** — Sept 1 2019).
  - `BTCUSDT-1h-2025-09.zip`: `open_time` = `1756684800000000` (16
    digits, **microseconds** — Sept 1 2025).

  Nothing in the file, filename or any docs page flags which unit a
  given file uses. We detect it by magnitude: a millisecond epoch
  timestamp doesn't reach `1e14` until the year 5138; a microsecond one
  passes `1e14` for any date after 1973 — comfortable margin either
  way. Implemented in
  `data/sources/binance_bulk.py::_detect_timestamp_unit`, with both
  real samples committed as regression fixtures at
  `tests/fixtures/binance/`.

- **CSV has no header row**, exactly the 12 documented kline columns,
  confirmed by inspecting both files directly.

- **Checksum format**: each `.zip` has a sibling `.zip.CHECKSUM` file
  containing `"<sha256-hex>  <filename>"` (`sha256sum -c` format).
  Verified by downloading a real file + its checksum and confirming a
  match (`b0ad5b54...` for `BTCUSDT-1h-2025-09.zip`).

- **Binance's own `close_time` is an inclusive endpoint** (one tick
  before the next bar's open), not the exclusive "when this bar
  finished" boundary `core.bar.Bar` uses everywhere else in this repo.
  We discard Binance's `close_time` entirely and recompute it as
  `open_time + interval`.

- A month that 404s is normal (not yet published — typically the
  current or previous month) and is handled as a skip-with-log-line,
  not an exception.

## Kraken (via ccxt, public OHLCV only)

- **The REST OHLC endpoint caps out at 720 of the most recent candles,
  full stop** — confirmed against Kraken's own REST docs. Passing
  `since` doesn't reach further back; there's no pagination path to
  deep history. This is why Kraken is the live/sim venue but Binance's
  bulk dumps supply the crypto backfill (spec Section 3.3 called this
  out; we verified it rather than assuming).

- **The last candle returned is always the current, still-forming
  one** ("will always be present" per Kraken's docs) and must be
  dropped — otherwise we'd violate design rule 3 (never act on an
  incomplete candle). Implemented in
  `data/sources/ccxt_ohlcv.py::fetch_recent_ohlcv`.

- ccxt's unified OHLCV timestamps are milliseconds; `limit` and
  `timeframe` strings (`"1h"`, `"4h"`, `"1d"`) match our own
  `core.enums.Timeframe` values directly, no translation needed.

## Alpaca (via `alpaca-py`)

- **Historical equities data requires an API key+secret even on the
  free plan.** Confirmed from Alpaca's docs: "With the exception of
  historical crypto data, all market data endpoints require
  authentication." This is a data-access credential, not a
  live-trading one (design rule 4 only gates *live* keys before Phase
  7) — but it's a real account you have to create. See
  `.env.example`.

- **The free-tier full-market feed is `DataFeed.DELAYED_SIP`, not
  `DataFeed.SIP`.** Read `alpaca-py`'s actual source
  (`alpaca/data/enums.py`): the `DataFeed` enum's own docstring says
  *"OTC and SIP are available with premium data subscriptions"* — plain
  `sip` is a paid feed. `delayed_sip` (SIP delayed 15 minutes) is the
  free-tier equivalent, which happens to be exactly what the spec asks
  for ("fetched at least 15 minutes after the close so the full-market
  bar is available on the free plan"). Using `DataFeed.SIP` on a free
  account would likely be rejected or silently restricted; we never
  tested that path since we found the right enum value first.

- `Adjustment.ALL` gives split- *and* dividend-adjusted prices, matching
  the spec's "split- and dividend-adjusted daily bars."

- `StockHistoricalDataClient.get_stock_bars()` returns a `BarSet` whose
  confirmed, documented attribute is `data: Dict[str, List[Bar]]`,
  keyed by symbol. We build the unified schema directly from that dict
  rather than from the `.df` convenience property, whose exact shape we
  could not confirm from source in the time available — safer to build
  from the documented attribute than assume.

- **Not yet verified** (no Alpaca account keys available while building
  Phase 0): the exact time-of-day convention of a daily bar's
  `timestamp` field (session open? close? UTC midnight?). `data/validate.py`'s
  equity-specific gap check (`find_missing_sessions`) deliberately
  compares by calendar date, not exact timestamp, to stay correct
  regardless of which convention turns out to be true. Revisit once
  real data has been fetched — see `docs/progress.md`.

## Kraken fees (verified during Phase 2, for `execution/costs.py`)

**Kraken's current entry-tier (Tier 1, $0+ 30-day volume) spot fees are
0.40% maker / 0.80% taker** — fetched directly from Kraken's live Pro
fee schedule while building the cost model. The spec's Section 6.1
table assumed 0.25% maker / 0.40% taker, exactly double the real
current rate. Fee schedules move; this is exactly why rule 3 says
verify rather than trust a number already in a document, even one
written as part of this project's own spec. `execution/costs.py`'s
`CostModel` default uses the verified 0.80% taker rate (Phase 2's order
manager only ever generates marketable/taker orders — see that
module's docstring for why maker isn't modeled).

Full tier table (for whenever volume-tiered fee modeling matters):
Tier 2 ($2.5K+) 0.30%/0.60%, down to Tier 17 ($500M+) 0.00%/0.05%. Not
transcribed in code — one entry-tier default is enough for a $10,000
paper sleeve that will never reach Tier 2 volume.

## Alpaca commissions (verified during Phase 2, for `execution/costs.py`)

**Confirmed commission-free**, from Alpaca's own commission
disclosure: "self-directed individual cash brokerage accounts that
trade U.S.-listed securities... through an API" (this project's exact
shape) pay no commission. "Relevant regulatory fees may apply" — the
standard tiny SEC/FINRA pass-through every US broker charges on equity
trades, not Alpaca-specific; small enough not to model as a separate
line item.

**Not confirmed**: whether funding or trading from a non-USD (e.g.
Belgian/EUR) account carries an FX fee. Alpaca's full Brokerage Fee
Schedule document (linked from their disclosures) wasn't reachable to
check during Phase 2. `execution/costs.py`'s equities `CostModel`
configuration models this as zero — flagged here as the one piece of
the cost model that's an assumption, not a verified fact, pending
either reaching that document or asking Alpaca support directly.

## Alembic / environment scripts

Alembic's `migrations/env.py` is the one place in this repo that
constructs `Settings()` from inside what is, in effect, a different
process entrypoint (the `alembic` CLI, not `cli.py`) — consistent with
the "construct once, at process start, never at import time" rule
(`config/__init__.py`), just a different process start.

## Yahoo Finance chart endpoint (Phase 3, equities research data)

Replaces Alpaca (no account; ADR 0004). Verified on real responses,
committed as fixtures in `tests/fixtures/yahoo/`:

- `GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1=&period2=&interval=1d&events=div,split`
  works without a key with a plain browser User-Agent. **Stooq**, the
  other obvious free source, now answers non-browsers with a JavaScript
  proof-of-work page, so it's unusable from a server.
- **Daily timestamps are the session *open*** in UTC: 13:30/14:30 for
  NYSE (DST-dependent), 08:00 for LSE. They're not the close and not
  midnight. `close_time` comes from the exchange calendar instead, early
  closes included.
- **The still-trading session is returned mid-session.** On 2026-11-20
  at ~11:00 UTC, VUSD.L had a bar stamped 08:00 that day with ~10% of a
  normal day's volume. It's dropped by comparing the calendar's session
  close with `now`.
- `indicators.adjclose` is split- and dividend-adjusted; O/H/L are
  scaled by the same per-bar `adjclose/close` factor.
- Unknown symbol: HTTP 404 with `{"chart": {"result": null, "error": {...}}}`.
- Data quality on the LSE USD-line UCITS twins is mediocre: one missing
  session (2025-10-24) on most twins, plus 2026-03-06 on EQQU and IDEM,
  and many zero-volume days (EQQU: 275 since 2016).

## IBKR commissions (Phase 3, unverified)

IBKR's own pricing pages return HTTP 403 to automated fetches. The
commission in `config/sleeves/equities.yaml` (tiered: 0.05% of value,
min USD 1.70, plus ~USD 0.21 exchange/clearing) comes from a
third-party summary (Jan 2025) of IBKR Pro for USD-denominated ETFs on
the LSE. **Confirm against your account's pricing page.** At a $10,000
sleeve the *minimum* dominates every order under ~$3,400, which is why
buffering matters so much for the equities sleeve (see the Phase 3
reports' buffering section).

## Binance Spot Testnet (Phase 4, via ccxt 4.5.92)

- `ccxt.binance(...).set_sandbox_mode(True)` swaps the spot REST base to
  `https://testnet.binance.vision/api/v3` (read in ccxt's `binance.py`
  `urls['test']`). ccxt also has a separate `demo` URL set
  (`demo-api.binance.com`); we use the testnet, as the spec says.
- Public endpoints need no key: `fetch_time`, `load_markets`,
  `fetch_ohlcv` all worked from this server. BTCUSDT filters on the
  testnet match production (tick 0.01, step 0.00001, min notional 5).
- **The newest kline is the still-forming one** (its `closeTime` is in
  the future), so it's dropped the same way as Kraken's.
- **Testnet prices are synthetic and wild**: one real 4h BTC bar on
  2026-11-20 had a low of 51,446 against ~61,900 open/close, a 17%
  wick. Plumbing only; never judge fills or strategy on it.
- `newClientOrderId` must match `^[.A-Z:/a-z0-9_-]{1,36}$`. Our
  deterministic ids are ~50 chars, so we send a **UUIDv5 of the full
  id** (`execution/brokers/client_ids.py`): deterministic, 36 chars,
  accepted by Kraken's `cl_ord_id` too.
- ccxt's binance `fetch_order(None, symbol, {"origClientOrderId": id})`
  looks an order up by client id, filled orders included (read in
  source). That's what makes "5xx -> look up before retrying" possible.
  The uniqueness rule for client ids applies among *open* orders only,
  so blind retries after a fill could duplicate an order.

## Kraken via ccxt (Phase 4)

- ccxt maps `params={"clientOrderId": ...}` to Kraken's `cl_ord_id` on
  create and cancel. There's no single-call lookup by `cl_ord_id` in
  ccxt's kraken, so the adapter scans open, then closed, orders.
- `precisionMode == TICK_SIZE` (verified for both Kraken and Binance in
  ccxt 4.5.92), so `market.precision.price` *is* the tick. Kraken
  BTC/USD: tick 0.1, step 1e-8, `limits.cost.min` 0.5 USD,
  `limits.amount.min` 6e-5 BTC.

## Interactive Brokers via ib_async 2.1 (Phase 4)

Read in the installed source (no gateway on this server yet):

- `IB.connect(host, port, clientId, timeout, readonly, account)`;
  Gateway ports are 4002 (paper) and 4001 (live).
- `IB.placeOrder(contract, LimitOrder(action, qty, lmtPrice, orderRef=..., tif=..., account=...))`
  returns a live-updating `Trade`. `orderRef` is free text, so it carries
  our full client id.
- Status strings: `Filled`; `Cancelled`/`ApiCancelled`; `Inactive`
  (rejected or held by IBKR risk); everything else is in flight.
  `OrderStatus.DoneStates = {Filled, Cancelled, ApiCancelled, Inactive}`.
- After a restart `IB.trades()` only knows the current session, so the
  adapter also checks `reqAllOpenOrders()` and
  `reqCompletedOrders(apiOnly=True)`, plus `reqExecutions()` for fills
  (`execution.execId` dedupes, `commissionReport.commission` is the fee).
- `reqHistoricalData(contract, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH, formatDate)`:
  historical bars need the same market-data subscription as live quotes.
- **Not yet verified:** which contract IBKR resolves for each UCITS twin
  (`Stock(symbol, "SMART", "USD", primaryExchange="LSEETF")`). Run
  `kestrel ibkr contracts` against the gateway. It refuses anything but
  exactly one match.

## prometheus-client

`start_http_server(port)` binds **`0.0.0.0` by default**. We always pass
`addr` explicitly (`KESTREL_METRICS_ADDR`, default `127.0.0.1`).
