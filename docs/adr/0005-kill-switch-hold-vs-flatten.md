# ADR 0005: On a kill-switch trip, cancel and hold (flattening is opt-in)

**Status:** accepted (Phase 4). Spec Section 6.3 [DECISION].

## Context

A kill switch trips when something is wrong: a daily loss or drawdown
limit, stale data, a ledger/venue mismatch, a burst of broker errors,
clock drift. Every trip cancels open orders and alerts. The question is
what happens to *existing positions*.

## Options

1. **Cancel and hold** (default). Stop adding risk; keep what's held.
2. **Cancel and flatten.** Sell everything at market (marketable limits)
   immediately.

## Decision

Hold by default; `risk.flatten_on_trip: true` per sleeve enables
flattening. When enabled, the bot flattens **once, on the cycle the
switch trips**, through the normal marketable-limit path with its own
client-id label (`{sleeve}-flatten-{ts}-{symbol}-{seq}`).

## Why hold is the safer default

- **Most trips aren't about the market.** Stale data, reconciliation
  mismatches, error bursts and clock drift mean *our view of the world
  is unreliable*. Trading on an unreliable view, a large one-sided
  trade included, is exactly what the switch exists to prevent.
  Flattening on a reconciliation mismatch means selling quantities we
  may have wrong.
- **Selling into the move that tripped a loss limit realises the loss
  at the worst moment**, on the thinnest liquidity (a 4h crypto bar
  after a crash; an LSE USD-line ETF), and turns a paper drawdown into a
  taxable event (Belgian TOB on the sale).
- **The strategies are long-only and unlevered** (design rule 1), so
  holding can't produce a margin call or a loss bigger than the
  position. The worst case of holding is bounded.

## Consequences

- After a trip, a human decides (runbook): reset and let the strategy
  resume, or flatten by hand (`kestrel admin pause`, then sell on the
  venue and `rebaseline`).
- A drawdown kill switch with hold doesn't cap losses at the limit; it
  stops *new* risk. Whoever enables live trading should know that.
- `flatten_on_trip` is tested (`tests/unit/live/test_sleeve_runner.py`)
  so the option works when chosen, but it's off in both sleeve configs.
