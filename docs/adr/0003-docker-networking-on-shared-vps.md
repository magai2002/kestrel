# 0003: Docker networking policy on a VPS with existing public services

## Context

Phase 0 needed Docker (for local Postgres, and later the full prod
stack in Phase 5) on a VPS that was not already running it. This same
VPS runs public-facing Nginx, WireGuard, Pi-hole, Fail2ban and several
other services (spec Section 2), fronted by an **active UFW**
firewall with explicit allow rules for exactly the ports those
services need (22, 80, 443, 51820/wg, 53, 8080/pihole, 3000/grafana,
9090/prometheus).

Docker Engine, by default, manages its own `iptables` rules (a
`DOCKER` chain and per-published-port `DNAT` rules) to make container
ports reachable. This is a well-documented interaction: Docker's rules
are inserted ahead of UFW's in the packet-filtering path, so a
container port published to `0.0.0.0` becomes reachable from the
public internet **regardless of what UFW says about it** — UFW has no
visibility into Docker's own chain. Installing Docker carelessly on a
box with a carefully-configured UFW is a known way to accidentally
expose something.

## Options considered

1. **Install Docker with its default `iptables` integration left on,
   but never publish a container port to `0.0.0.0`** — every published
   port binds to `127.0.0.1` explicitly, or isn't published at all.
2. Same as (1), plus explicit `DOCKER-USER` chain rules (or the
   `ufw-docker` community tool) making UFW the authority over
   Docker-originated traffic too, as defense-in-depth.
3. **Disable Docker's `iptables` management** (`"iptables": false` in
   `daemon.json`) and hand-manage every rule Docker would otherwise
   create.
4. Don't install Docker on this box at all; run Postgres (and later
   the engine/API) as native systemd services instead, matching how
   the existing market-data collector and travel-deal agent run.

## Decision

Option 1, confirmed with the project owner before installing anything
(per spec Section 0, rule 5: ask before changing anything on the server
outside this project's directories). No `DOCKER-USER` hardening beyond
that for now.

## Rationale

- **Binding to loopback isn't a firewall rule Docker can route around
  — it's a property of the listening socket.** A `127.0.0.1`-bound
  port is not reachable from any network interface at all, independent
  of whatever `iptables`/`DOCKER` chain rules exist. This sidesteps the
  UFW-bypass problem entirely rather than trying to out-configure it.
- This is already the project's own standing policy, not a new rule
  invented for Docker: Section 2 says every production port binds to
  `127.0.0.1` only, and the local dev Postgres container goes further
  — the spec's suggested prod defaults give Postgres no host port at
  all. `docker-compose.yml` publishes `127.0.0.1:5432:5432` for local
  dev convenience (so host-side `alembic`/`psql` can reach it); prod
  compose (Phase 5) follows the spec's stricter "no host port" guidance
  for Postgres.
- Option 2's `DOCKER-USER` hardening is real defense-in-depth but adds
  an extra set of rules to maintain correctly across every future
  service this project adds, for a risk already closed by option 1. If
  a future phase needs a container port reachable from outside
  localhost for a legitimate reason, *that* is the point to add
  `DOCKER-USER` rules or an `ufw-docker`-style fix, not before.
- Option 3 (`iptables: false`) trades one footgun for another: it also
  disables the NAT rules containers need to reach the outside world via
  the `docker0` bridge, which would need to be hand-rebuilt correctly
  to avoid breaking outbound container networking (image pulls, the
  engine's own calls to Binance/Kraken/Alpaca).
- Option 4 (no Docker) only delays the decision — Phase 5's prod
  deployment explicitly runs `engine-sim`, `engine-sandbox`, `api` and
  `postgres` via `docker compose`, so Docker becomes unavoidable then
  regardless.

## Consequences

- Every `ports:` entry in every compose file in this repo, forever,
  must bind to `127.0.0.1` explicitly (`"127.0.0.1:PORT:PORT"`), never
  a bare `"PORT:PORT"` (which Docker Compose publishes to `0.0.0.0` by
  default). Code review for any new compose service should check this
  specifically — it's the one mistake that silently reopens the risk
  this ADR closes.
- If a later phase genuinely needs a container reachable from outside
  localhost (e.g. the public dashboard's API container, Phase 6), that
  traffic goes through the **host's** Nginx (already in UFW's allow
  list for 80/443) reverse-proxying to the container's loopback-bound
  port — the container itself never gets a public-facing bind.
