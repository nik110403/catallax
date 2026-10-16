#!/usr/bin/env bash
# Nightly demo reset (DEMO-SPEC §2.5): wipe postgres + redis, bring the stack
# back, seed the persona content, re-lock the app, then warm the caches.
# Run by catallax-demo-reset.service (03:30). Idempotent; safe to run by hand.
#
# Seeding needs writes, so the backend is started with DEMO_READONLY=false for
# the seed only and then recreated read-only. The edge proxy refuses non-GET
# methods the whole time and the LAN port is not forwarded, so the window is
# not reachable from the internet.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STACK="$(cd "$HERE/../.." && pwd -P)"
DC="$HERE/compose.sh"
cd "$STACK"

"$HERE/preflight-demo.sh"

"$DC" down
# Only the two stateful volumes; images and the rest are untouched.
docker volume rm catallax-demo_postgres-data catallax-demo_redis-data 2>/dev/null || true

DEMO_READONLY=false "$DC" up -d --wait
NGINX_HOST_PORT="${DEMO_SEED_PORT:-8191}" "$STACK/bin/seed-demo.sh"

# Recreate with the default (read-only) flag, then verify the lock really is on.
"$DC" up -d --force-recreate --wait fastapi-backend
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:${DEMO_SEED_PORT:-8191}/api/v1/watchlist" \
  -H 'Content-Type: application/json' -d '{}')"
[ "$code" = 403 ] || { echo "reset-demo: write probe returned $code, expected 403" >&2; exit 1; }

# Redis was wiped: refill the caches so the demo is not empty until the next timer tick.
"$DC" --profile warm run --rm demo-warm
echo "reset-demo: done"
