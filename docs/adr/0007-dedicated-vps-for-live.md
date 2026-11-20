# ADR 0007: Move the engine to a dedicated small VPS before trading real money

**Status:** proposed (Phase 7; spec Section 2 [DECISION] says to write
this before live mode). **Recommendation: yes, a dedicated box for
`live`, and also for `sandbox` once the IB Gateway is in use.**

## Context

`sim` runs today as Docker containers on the shared Hetzner box. That
box also runs:
- public-facing Nginx for alexeymagay.com
- WireGuard and Pi-hole/Unbound (DNS for your devices)
- Grafana, Prometheus and InfluxDB
- a market-data collector and a travel-deal agent

It has **3.7 GiB RAM, 2 vCPUs and no swap**. VS Code server alone takes
~1.5 GiB while you're connected (Phase 5 measurements).

Live mode adds three things that change the risk calculus:
1. **Credentials that can move money**: Kraken trading keys, and an
   IBKR login whose gateway can place any order the account permits.
2. **A Java process with a large footprint**: IB Gateway, 600–900 MB.
3. **Availability matters**: a missed decision is a missed trade, and an
   unresponsive engine during a kill-switch event is worse.

## Options

1. **Stay co-located** with tighter limits. Zero cost, but the blast
   radius stays the same: anything that compromises Nginx, the VPN or
   another service on the box reaches the trading keys. Memory pressure
   with no swap risks the kernel OOM-killing *something*, possibly the
   VPN, during a live order.
2. **Dedicated small VPS** (e.g. a Hetzner CX22-class box: 2 vCPU,
   4 GB; a few euros a month) running only Postgres, the engines and the
   IB Gateway. No public ports at all: no Nginx, and SSH only over
   WireGuard. The dashboard API can stay on the shared box, reading a
   replica or a nightly export, or move too.
3. **Managed services** (managed Postgres, container hosting). More
   moving parts and more credentials in more places, for no real gain at
   this scale.

## Decision (recommended)

Option 2, before `live` is unlocked:
- **Engine box:** `engine-live`, `engine-sandbox`, `ib-gateway` and
  Postgres. It has a static IP, which is also what Kraken's API-key IP
  whitelist and IBKR's trusted-IP settings will point at (gate 5).
- **Shared box:** keeps `engine-sim`, the public API and the dashboard.
  `sim` has no credentials, so co-locating it costs nothing in risk.
- **No inbound internet traffic** to the engine box. Outbound only:
  Kraken, IBKR, ntfy. Admin over WireGuard.
- Run the same `deploy/docker-compose.prod.yml` with a `live` profile
  (added when this is accepted), plus the same backups and runbook.

## Consequences

- One more box to patch, and its own backups (the timer and script are
  identical).
- The 8-week `sim` history gate (gate 3) keeps accruing on the shared
  box. The tracking comparison doesn't care where `sim` runs.
- The public dashboard's `/api` needs the live engine's equity, so
  either the API moves too, or the engine box pushes a nightly
  normalised export. That decision is deferred until live is actually
  unlocked; until then there is no live data to show.
- **This ADR stays "proposed" until you accept it**: it's a paid service
  (spec rule 5), and its IP becomes part of the key restrictions.
