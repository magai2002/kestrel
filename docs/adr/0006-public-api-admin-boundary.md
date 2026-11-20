# ADR 0006: Public API and admin API are separate apps on separate ports

**Status:** accepted (Phase 6). Spec Section 8.1.

## Context

The dashboard needs a public, read-only API on the internet. Operating
the bot needs admin actions: pause/resume a sleeve, reset a kill switch,
see full state including balances. Admin actions are exactly what an
attacker would want, and "we'll protect it with a token" is one
misconfiguration away from exposing it.

## Options

1. One FastAPI app, an `/admin` router protected by a token.
2. One app, `/admin` blocked at Nginx.
3. **Two apps, two processes, two ports** *(chosen)*: the public app
   proxied by Nginx; the admin app on a loopback port that Nginx never
   proxies.

## Decision

- `create_public_app` contains **no admin routes at all**, rather than
  routes that happen to be blocked (`tests/unit/api/test_api.py` checks
  that `/admin/...` 404s on it). It returns only shareable numbers:
  equity indexed to 100, weights in %, prices, slippage and fees in bps.
  Never balances, quantities, notionals or account ids, and a test seeds
  known absolute values and checks none of them appear. It's
  rate-limited in Nginx and again in the app (token bucket per client;
  `X-Forwarded-For` trusted only from configured proxy ranges).
- `create_admin_app` runs as its own container (`api-admin`), published
  on `127.0.0.1:8711` only, and also requires a bearer token
  (`KESTREL_ADMIN_TOKEN`). The CLI refuses to bind it off loopback
  without one. Reach it over SSH (`ssh -L 8711:127.0.0.1:8711 server`)
  or WireGuard.
- The `kestrel admin` CLI does the same operations directly against the
  database, so the admin API is a convenience, not a dependency.

## Consequences

- The public attack surface is a read-only app that can't change
  anything even if fully compromised: its DB role could be made
  read-only too (follow-up, not done yet).
- Admin actions take effect at the runner's next cycle (they write the
  same DB rows the runner reads), not instantly. That's acceptable for
  4h/daily bars, and documented.
- "Trigger reconciliation" is an explicit no-op endpoint: reconciliation
  needs the engine's broker connection and already runs every cycle.
