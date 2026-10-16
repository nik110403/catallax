#!/usr/bin/env bash
# Scheduled cache warm (catallax-demo-warm.service): one demo-warm run, then stop the
# openbb-data sidecar so it holds no memory between warms. demo-warm starts it itself
# (depends_on) and polls its health; the macro step is optional, quotes/bars do not wait on it.
# Same lock as reset-demo.sh (which runs the same warm inline): never overlaps with the reset.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STACK="$(cd "$HERE/../.." && pwd -P)"
DC="$HERE/compose.sh"
exec 9>"$STACK/.demo-lock"
flock -w 600 9 || { echo "warm-demo: could not take $STACK/.demo-lock (reset or warm still running)" >&2; exit 1; }

rc=0
"$DC" --profile warm run --rm demo-warm || rc=$?
# Always stop the sidecar, also after a failed warm; its own failure must not mask the warm status.
"$DC" --profile warm rm -sf openbb-data || echo "warm-demo: could not stop openbb-data" >&2
exit "$rc"
