# Chaos drills

`test_chaos.py` runs the spec's chaos scenarios automatically against
`FakeVenue`'s fault injection (CI runs it). The same scenarios are worth
running by hand against the real `sandbox` deployment once, before
trusting it unattended. They're also rehearsal for Phase 7's "emergency
stop has been rehearsed in sandbox" gate.

Each drill passes if, afterwards, `kestrel admin status` shows **no
`reconcile_mismatch`**, the positions match the venue, and the
`orders` table has no client id sent twice:

```sql
SELECT client_order_id, count(*) FROM orders WHERE mode = 'sandbox'
GROUP BY 1 HAVING count(*) > 1;   -- must return nothing
```

## 1. Kill the engine between send and ack

Watch the logs for the next decision (`docker compose -f
deploy/docker-compose.prod.yml logs -f engine-sandbox`). The moment an
`order` event appears, before its `ack`:

```sh
docker kill kestrel-engine-sandbox-1   # SIGKILL: no clean shutdown
```

`restart: unless-stopped` doesn't apply to an explicit `docker kill`, so
start it again: `docker compose -f deploy/docker-compose.prod.yml up -d
engine-sandbox`. Expected: the start-up catch-up cycle finds the
`intent` row in `orders`, looks the client id up at the venue, books its
fills, and does **not** re-send.

## 2. Drop the network mid-order

```sh
docker network disconnect kestrel_default kestrel-engine-sandbox-1
sleep 90
docker network connect kestrel_default kestrel-engine-sandbox-1
```

Expected: transient errors in the log, possibly an `error_burst` kill
switch (reset it by hand: that's the point), no duplicate orders, and a
clean reconciliation on the next cycle.

## 3. Restart mid-bar

`docker compose -f deploy/docker-compose.prod.yml restart engine-sandbox`
between two bar closes. Expected: the catch-up cycle reports
`no_new_bar` and sends nothing.

## 4. Binance testnet wipe (happens on its own, periodically)

Expected: `reconcile_mismatch` trips and you get an ntfy alert. Recover with:

```sh
kestrel admin rebaseline --mode sandbox --sleeve crypto
kestrel admin reset-kill-switch --mode sandbox --sleeve crypto --name reconcile_mismatch
```
