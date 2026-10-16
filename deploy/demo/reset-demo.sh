#!/usr/bin/env bash
# Nightly demo reset (DEMO-SPEC §2.5): wipe postgres + redis, bring the stack
# back READ-ONLY, check that writes are refused, then warm the caches.
# Run by catallax-demo-reset.service (03:30). Idempotent; safe to run by hand.
#
# The demo is never unlocked: DEMO_READONLY is pinned to true in compose.demo.yml
# and nothing here changes it. The sample artifacts (DEMO-SPEC §13) come from the
# one-shot `demo-seed` service, which `up --wait` runs on the wiped database before
# the backend starts serving; this script does no seeding of its own. Config comes from the stack
# `.env` (compose reads it itself), not from the caller's shell environment.
#
# A flock on $STACK/.demo-lock keeps reset and warm from running concurrently
# (catallax-demo-warm.service takes the same lock).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STACK="$(cd "$HERE/../.." && pwd -P)"
DC="$HERE/compose.sh"
cd "$STACK"

# Read KEY from the stack .env (last assignment wins); never from the shell.
env_val() { sed -n "s/^$1=//p" "$STACK/.env" 2>/dev/null | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"; }
[ -f "$STACK/.env" ] || { echo "reset-demo: $STACK/.env missing (copy deploy/demo/demo.env.example)" >&2; exit 1; }

exec 9>"$STACK/.demo-lock"
flock -w 1800 9 || { echo "reset-demo: could not take $STACK/.demo-lock (warm or reset still running)" >&2; exit 1; }

DEMO_DENY_MARKERS_FILE="$(env_val DEMO_DENY_MARKERS_FILE)"
export DEMO_DENY_MARKERS_FILE
"$HERE/preflight-demo.sh"

# Until the write probe has passed, any failure takes the stack down: a stack
# that is not provably read-only must not serve. After that, only warm can fail
# and the (read-only) stack stays up with whatever the caches hold.
LOCKED=0
cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$LOCKED" -ne 1 ]; then
    echo "reset-demo: failed (exit $rc) before the read-only check passed; taking the stack down" >&2
    "$DC" down || true
  fi
}
trap cleanup EXIT

"$DC" down
# Only the two stateful volumes; images and the rest are untouched.
# A volume that never existed is fine; one that survives `rm` is a failed wipe
# (the trap then takes the stack down).
for vol in catallax-demo_postgres-data catallax-demo_redis-data; do
  docker volume rm "$vol" >/dev/null 2>&1 || true
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    echo "reset-demo: volume $vol still exists after rm; wipe failed" >&2
    exit 1
  fi
done

"$DC" up -d --wait

# Write probe, run inside the backend container against its own port 8000
# (nothing else is published for the script; the LAN port belongs to the edge).
# Any non-GET on /api/v1/* must be answered 403 by the app middleware.
code="$("$DC" exec -T fastapi-backend python -c '
import urllib.request, urllib.error
req = urllib.request.Request("http://127.0.0.1:8000/api/v1/watchlist", data=b"{}", method="POST",
                             headers={"Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req).status)
except urllib.error.HTTPError as e:
    print(e.code)
')"
code="${code//[$'\r\n ']/}"
[ "$code" = 403 ] || { echo "reset-demo: write probe returned '$code', expected 403" >&2; exit 1; }
LOCKED=1

# Redis and postgres were wiped: refill caches and daily bars so the demo is
# not empty until the next timer tick.
"$DC" --profile warm run --rm demo-warm
echo "reset-demo: done"
