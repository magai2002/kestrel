# Runbook

Everything here runs from the repo root on the server
(`/root/trading_bot/kestrel`). `PROD` below means
`docker compose -f deploy/docker-compose.prod.yml`.

## Start, stop, status

```sh
$PROD up -d                                   # postgres -> migrate -> engine-sim
$PROD --profile sandbox up -d                 # + ib-gateway + engine-sandbox (needs credentials, below)
$PROD stop engine-sim                         # stop one engine (positions are held; nothing is sold)
$PROD down                                    # stop everything (data volume kept)
$PROD exec engine-sim kestrel admin status    # heartbeat, last bar, positions, tripped kill switches
$PROD logs -f --tail 100 engine-sim           # JSON logs (one "cycle" line per decision)
curl -s 127.0.0.1:9710/metrics | grep ^kestrel_
```

A reboot brings everything back on its own: every service is
`restart: unless-stopped`, and Docker starts at boot. On start, each
engine runs a catch-up cycle immediately, which recovers any order a
crash left half-sent.

## Deploy and roll back

```sh
./deploy/deploy.sh                                # on the box: git pull, build, migrate, restart, smoke test
make deploy DEPLOY_HOST=root@<server>             # from the laptop, over SSH
KESTREL_IMAGE_TAG=<sha> KESTREL_GHCR_REPO=<owner>/kestrel ./deploy/deploy.sh   # a CI-built image
```

**Roll back:** `git checkout <previous sha> && ./deploy/deploy.sh`, or
redeploy an older GHCR tag. Migrations are forward-only in practice:
before rolling back across one, take a backup and check that its
`downgrade()` doesn't drop data you need.

## Emergency stop

Rehearse this in `sandbox` before Phase 7 (spec Section 13, gate 7).

1. `$PROD exec engine-sandbox kestrel admin pause --mode sandbox --sleeve all`.
   No new decisions from the next cycle on; open orders are left alone.
   If you can't wait for the next cycle, do step 2 straight away.
2. `$PROD stop engine-sandbox`. The process stops; nothing is sent, and
   anything resting at the venue stays resting.
3. Cancel resting orders **at the venue** (Binance testnet UI; IBKR
   Client Portal or TWS).
4. If you want out of the positions, sell them **at the venue** by
   hand, then `kestrel admin rebaseline` before restarting so
   reconciliation accepts the new holdings.

Pausing doesn't flatten, by design (ADR 0005).

## Kill switch tripped

ntfy alert: `[mode/sleeve] KILL SWITCH <name>`. The engine has already
cancelled open orders and stopped deciding; positions are held. **It
will not reset itself.**

| Switch | What it means | What to do |
| --- | --- | --- |
| `daily_loss` | Sleeve lost more than its daily limit (3%) since the UTC day's first bar | Look at what moved. Reset when you're comfortable with the strategy carrying on. In crypto 3% days are common; see docs/progress.md |
| `drawdown` | More than 20% below the sleeve's peak | Don't reset reflexively: this is the "strategy may be broken" alarm |
| `stale_data` | Newest closed bar older than 2 bars (crypto) or 2 sessions (equities) | Check the feed (Kraken/Yahoo/IBKR up? gateway logged in?). Reset once bars flow |
| `reconcile_mismatch` | Venue holdings or cash disagree with the ledger | See below. Never reset without understanding why |
| `error_burst` | 5+ broker or feed errors in 15 min | Read the errors in the `cycle` log lines. Reset after fixing the cause |
| `clock_drift` | Venue time and ours differ by more than 5 s | `timedatectl`; check `systemd-timesyncd` |

```sh
$PROD exec engine-sim kestrel admin reset-kill-switch --mode sim --sleeve crypto --name <name> --by "<you: why>"
```

## Reconciliation mismatch

The ledger expects `venue == baseline + ledger`. The usual causes:
- the **Binance testnet was wiped** (it happens periodically);
- someone traded in the account by hand;
- a fill the engine never saw, which would be a bug: report it.

```sh
$PROD exec engine-sandbox kestrel admin status
$PROD exec engine-sandbox kestrel admin rebaseline --mode sandbox --sleeve crypto   # accept the venue's holdings
$PROD exec engine-sandbox kestrel admin reset-kill-switch --mode sandbox --sleeve crypto --name reconcile_mismatch --by "<you>"
```

For IBKR paper: the sleeve trades USD-denominated LSE lines. If the
paper account holds only EUR, cash reconciliation trips at once
(correctly). Convert EUR to USD in the paper account first (IdealPro FX).

## Engine heartbeat missing

The container is down or hung: `$PROD ps`, then
`$PROD logs --tail 200 engine-sim`. An OOM kill shows `Exit 137` and a
`Memory cgroup out of memory` line in `dmesg`; see the memory budget.

## Data stale

Kraken, Yahoo or IBKR isn't delivering. Check the `errors` field of the
latest `cycle` log line. Yahoo is an unofficial endpoint: if its shape
changed, `YahooShapeError` says so loudly; fix `data/sources/yahoo_chart.py`
against a freshly recorded response, following the fixture pattern.

## IB Gateway (sandbox)

- Credentials: `cp deploy/ibgateway.env.example deploy/ibgateway.env && chmod 600 deploy/ibgateway.env`,
  and fill in your **paper** username and password. They're read only by
  the gateway container.
- The gateway restarts itself daily at 23:59 UTC (`AUTO_RESTART_TIME`).
  Paper logins normally need no 2FA; **live** logins need your phone
  approval after each weekly re-authentication (Phase 7).
- Verify the UCITS twins resolve:
  `$PROD --profile sandbox run --rm engine-sandbox ibkr contracts`.
- The image tag is pinned (`10.50.1h`): upstream moves from IBC to
  "ibcontroller", with breaking env changes, no earlier than 2027-01-10.

## Rotate keys

1. Create the new key at the venue (Binance testnet: trade-only, IP-restricted where possible).
2. Edit `.env` (chmod 600), then `$PROD up -d engine-sandbox` to recreate the container with the new env.
3. Delete the old key at the venue.
4. For IBKR: change the password at IBKR, update `deploy/ibgateway.env`, `$PROD --profile sandbox up -d ib-gateway`.

## Backups and restore

Nightly `pg_dump` (custom format) to `/var/backups/kestrel`, kept 14
days, via `deploy/backup/kestrel-backup.{service,timer}` (install
commands are in the service file). Each dump is verified with
`pg_restore --list` before it's kept. Manual run: `make backup`.

**Restore** (tested on 2026-11-20 against a scratch database):

```sh
$PROD stop engine-sim engine-sandbox
$PROD exec -T postgres dropdb -U kestrel kestrel
$PROD exec -T postgres createdb -U kestrel kestrel
$PROD exec -T postgres pg_restore -U kestrel -d kestrel --no-owner < /var/backups/kestrel/kestrel-<stamp>.dump
$PROD up -d
```

Market data (`market_data/`) isn't in the dump. It's rebuilt by
`make data`, except the bars the live feeds recorded. Back up that
directory too if the shadow-backtest history matters to you
(`tar czf` it to the same folder).

## Memory budget

The box has 3.7 GiB and **no swap**. VS Code server plus a Claude
session alone use about 2 GiB while connected. Container limits (hard
caps, so an engine can never starve WireGuard, Pi-hole or Nginx):

| Service | Limit | Typical use |
| --- | --- | --- |
| postgres | 512 MB | ~30 MB |
| engine-sim | 512 MB | ~190 MB |
| engine-sandbox | 512 MB | ~190 MB |
| ib-gateway (Java) | 1100 MB | 600–900 MB |
| migrate | 256 MB | one-shot |

One-off jobs (`kestrel validate`) run with the engine's 512 MB limit. A
bootstrap that materialised every resample at once was OOM-killed there
during Phase 5 and has since been fixed to work in chunks.

## File ownership gotcha

The containers run as uid 10001 and bind-mount `market_data/` and
`reports/`. Running a data command **on the host as root** (e.g.
`uv run kestrel data fetch-crypto`) creates root-owned files the
engine can't overwrite. Symptom: `Permission denied` in the cycle
errors, then an `error_burst` kill switch. Run data commands inside the
stack instead (`$PROD run --rm engine-sim data fetch-crypto`), or
`chown -R 10001:10001 market_data reports` afterwards (`deploy.sh` does
this).
