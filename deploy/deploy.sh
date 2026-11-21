#!/usr/bin/env bash
# Deploy on the server (SPEC Section 10): pull code, build or pull the
# image, run migrations, (re)start the stack, smoke-test. `make deploy`
# runs this over SSH; on the box itself run it directly.
#
#   KESTREL_IMAGE_TAG=<git sha> ./deploy/deploy.sh          # pull from GHCR
#   ./deploy/deploy.sh                                     # build locally (tag "local")
#   KESTREL_PROFILES=sandbox ./deploy/deploy.sh            # include ib-gateway + engine-sandbox
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f deploy/docker-compose.prod.yml)
for p in ${KESTREL_PROFILES:-}; do COMPOSE+=(--profile "$p"); done

git pull --ff-only

# The public API's SELECT-only DB role needs its own password (docs/adr/0008).
if ! grep -q '^KESTREL_PUBLIC_DB_PASSWORD=' deploy/.env; then
  echo "KESTREL_PUBLIC_DB_PASSWORD=$(openssl rand -hex 32)" >> deploy/.env
  chmod 600 deploy/.env
  echo "generated KESTREL_PUBLIC_DB_PASSWORD in deploy/.env"
fi
if [[ "${KESTREL_IMAGE_TAG:-local}" == "local" ]]; then
  "${COMPOSE[@]}" build
else
  docker pull "ghcr.io/${KESTREL_GHCR_REPO:?set KESTREL_GHCR_REPO, e.g. owner/kestrel}:${KESTREL_IMAGE_TAG}"
  docker tag "ghcr.io/${KESTREL_GHCR_REPO}:${KESTREL_IMAGE_TAG}" "kestrel:${KESTREL_IMAGE_TAG}"
fi

# The container user (uid 10001) must own the bind-mounted data dirs.
mkdir -p market_data reports
chown -R 10001:10001 market_data reports

"${COMPOSE[@]}" up -d --remove-orphans   # `migrate` runs first (depends_on)

echo "smoke test..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:9710/metrics | grep -q '^kestrel_last_success_unixtime'; then
    echo "engine-sim metrics OK"
    "${COMPOSE[@]}" ps
    exit 0
  fi
  sleep 5
done
echo "SMOKE TEST FAILED: engine-sim metrics not up after 150 s" >&2
"${COMPOSE[@]}" logs --tail 50 engine-sim >&2
exit 1
