# ADR 0004: Interactive Brokers for equities, UCITS twins, hybrid data

**Status:** accepted (2026-11-20, before Phase 3). Supersedes the Alpaca
parts of SPEC.md Sections 3.2–3.3.

## Context

The spec planned Alpaca for equities in every mode: data, paper
(`sandbox`) and live. Three facts changed that:

1. **The account holder has no Alpaca account and won't open one.** They
   have an Interactive Brokers (IBKR) account and want to use it for
   both paper and, eventually, real-money equities.
2. **EU retail clients can't buy US-domiciled ETFs.** SPY, QQQ, TLT etc.
   publish no PRIIPs Key Information Document, so EU-regulated brokers
   (IBKR Ireland included) block retail buy orders for them. The usual
   workaround is an Irish-domiciled UCITS ETF tracking the same (or a
   close) index.
3. **IBKR's API historical data needs the same paid market-data
   subscription as live quotes**, and it has strict pacing limits. A
   10-year daily backfill through it is slow and may need a subscription.

## Options

**Universe**

- **A. UCITS twins everywhere:** backtest, sim and live all on CSPX/VUSD
  etc. One instrument set, but shorter and thinner history (several
  twins only list from 2017–2024, and LSE USD lines trade thinly).
- **B. Keep the US ETFs:** works only for a professional-client account
  or in paper. Live orders would be rejected.
- **C. Research on the US ETFs, trade the UCITS twins** *(chosen)*.

**Equities data**

- **IBKR only:** one source, but slow backfill, possibly paid, and
  unavailable when the gateway is down.
- **Free source only:** not the price actually traded at.
- **Hybrid** *(chosen)*: a free, no-account source for the research
  backfill and the twins' warm-up history; IBKR's own bars for
  `sim`/`sandbox`/`live`.

## Decision

- **Equities broker: IBKR** for `sandbox` (paper account) and `live`,
  reached through **IB Gateway in Docker with IBC** (auto-login, daily
  restart), driven from Python by **`ib_async`** (the maintained
  successor of `ib_insync`). Credentials live only in the gateway
  container's environment; the engine connects to `127.0.0.1:4002`
  (paper) or `:4001` (live) and never sees the password.
- **Research universe:** the spec's ten US ETFs, daily, from 2016, from
  **Yahoo Finance's public chart endpoint** (`data/sources/yahoo_chart.py`).
  Stooq, the other obvious free source, now serves a JavaScript
  proof-of-work page to non-browsers.
- **Traded universe:** one UCITS twin per US ETF, USD line on the LSE,
  **distributing share class where one exists** (Belgian TOB: 0.12% vs
  1.32% for accumulating funds registered in Belgium, an 11× difference
  on every trade):

  | Research | Traded | Share class | Note |
  | --- | --- | --- | --- |
  | SPY | VUSD | Dist | Vanguard S&P 500 |
  | QQQ | EQQU | Dist | Invesco EQQQ Nasdaq-100 |
  | IWM | R2US | Acc | No distributing USD line found |
  | EFA | EXUS | Acc | MSCI World ex-USA, *includes Canada*; listed 2024 |
  | EEM | IDEM | Dist | Same index (MSCI EM) |
  | TLT | IDTL | Dist | |
  | IEF | IDTM | Dist | |
  | GLD | IGLN | ETC | A note, not a fund; TOB treatment unverified |
  | DBC | CMOD | Acc | *Different index* (Bloomberg Commodity) |
  | VNQ | IDUP | Dist | US property yield, not identical to VNQ |

- **Costs in research are the twins' costs**, not the US funds': IBKR's
  percentage-plus-minimum commission and the twin's TOB rate. That's
  what the bot will actually pay.
- **Crypto is unchanged:** Binance history, Binance Spot Testnet for
  `sandbox`, Kraken for `sim` prices and `live`.

## Consequences

- **The backtest isn't on the traded instruments.** Index differences
  (EXUS vs EFA, CMOD vs DBC), trading hours (LSE closes at 16:30
  London, four and a half hours before NYSE), FX and liquidity all
  separate them. The live-vs-shadow-backtest tracking page (Phase 6)
  replays the *twins'* actual bars, so the gap is measured, not
  assumed.
- **The live runner computes signals on the twins' own price history**,
  seeded from Yahoo (`VUSD.L` etc.) and then topped up from IBKR. Every
  asset id stays the research ticker (`SPY` means "the S&P 500 slot,
  currently traded as VUSD"), so strategy params mean the same thing in
  every mode.
- **Unverified assumptions, flagged in config and in every report:**
  IBKR's LSE commission (0.05%, min USD 1.70 tiered, from a third-party
  summary; IBKR's own page refuses automated fetches), each twin's TOB
  rate, and the contract details IBKR will resolve for each symbol
  (checked against a live gateway with `kestrel ibkr contracts`).
- **The sleeve is USD-denominated, but an EU IBKR account is probably
  EUR-based.** Buying a USD line without USD cash borrows USD on margin
  in a margin account, or is rejected in a cash account. Convert EUR to
  USD once (IdealPro) before the first paper/live trade; the runbook
  covers it.
- **Operational cost of IB Gateway:** a third-party Docker image, a
  stored IBKR password on the server (paper first), and for live a
  weekly 2FA re-login on the phone.
