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

## Alembic / environment scripts

Alembic's `migrations/env.py` is the one place in this repo that
constructs `Settings()` from inside what is, in effect, a different
process entrypoint (the `alembic` CLI, not `cli.py`) — consistent with
the "construct once, at process start, never at import time" rule
(`config/__init__.py`), just a different process start.
