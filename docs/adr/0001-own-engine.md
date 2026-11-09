# 0001: Build our own event-driven engine instead of adopting NautilusTrader

## Context

[NautilusTrader](https://www.nautilustrader.io/) is the open-source
reference implementation for backtest-live parity in Python/Rust
trading systems: one strategy codebase, a message bus, a shared cache,
and separate data/risk/execution engines that behave identically
whether they're replaying history or watching a live feed. It already
solves most of what `kestrel`'s Section 4 architecture is reaching for.

Adopting it would mean less code to write and a battle-tested, far more
complete implementation of exactly the hard part of this project
(no-look-ahead guarantees, realistic fills, order-state machines).

## Options considered

1. **Adopt NautilusTrader** as the engine; write `kestrel`'s strategies,
   risk rules and venue adapters against its interfaces.
2. **Build a smaller engine from scratch**, borrowing NautilusTrader's
   concepts (message bus, cache, separate data/risk/execution engines)
   where they fit, but owning every line.
3. **Fork/vendor an older stable NautilusTrader release** and freeze it.

## Decision

Option 2: build our own, smaller engine.

## Rationale

- **The goal of this project is to learn and demonstrate the
  mechanics**, not to ship the fastest path to a working bot (Section
  1's goals: "every module maps to a trading concept, and the code and
  docs explain it"). Adopting NautilusTrader would mean the most
  educationally valuable parts — the event loop, the no-look-ahead
  guarantee, the fill model, the order manager's reconciliation against
  broker state — are exactly the parts we'd no longer be writing or
  needing to fully understand.
- NautilusTrader is, as of this writing, mid-way through a major-version
  transition with breaking API changes across releases. Building
  against a moving target we don't control adds churn without adding
  learning value.
- A portfolio piece built by assembling someone else's production
  engine demonstrates integration skill; a portfolio piece built by
  writing the engine demonstrates the underlying trading-systems and
  distributed-state understanding a quant or AI-engineering hiring
  manager is actually screening for (Section 1's "show it off" goal).

## Consequences

- We will re-derive, more slowly and with more bugs along the way,
  problems NautilusTrader has already solved (partial fills, restart
  recovery, clock abstraction). The testing strategy in Section 11
  (property tests for no-look-ahead, parity tests against a vectorized
  backtester, chaos tests) exists specifically to catch the class of
  bug a mature engine would have already caught.
- We explicitly borrow NautilusTrader's *concepts* without its code:
  a message bus / event log (`core` events, Postgres `event_log`), a
  cache (the positions/equity ledger), and separate data, risk and
  execution concerns (`data/`, `risk/`, `execution/` as distinct
  packages per Section 4.2's repo layout).
- If `kestrel`'s own engine turns out to be insufficient for live
  trading (Phase 7), the fallback is to revisit this decision, not to
  have burned the ability to switch — the `Strategy`/`Broker`/`Clock`
  protocol boundaries (Section 4.3) are deliberately the same shape
  NautilusTrader uses, so a future migration wouldn't require rewriting
  strategies.
