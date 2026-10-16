#!/usr/bin/env bash
# Pinned compose wrapper for the demo stack: fixed project name and files, so
# systemd units and operators cannot drift from the preflighted configuration.
#   deploy/demo/compose.sh up -d --wait
#   deploy/demo/compose.sh --profile warm run --rm demo-warm
set -euo pipefail
STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$STACK"
# `depends_on: !override` / `ports: !override` need Compose >= 2.24.4; an older
# one would merge instead and could keep a wildcard port. Fail closed.
MIN_COMPOSE=2.24.4
ver="$(docker compose version --short 2>/dev/null || true)"
ver="${ver#v}"; ver="${ver%%[-+]*}"
if [ -z "$ver" ] || [ "$(printf '%s\n%s\n' "$MIN_COMPOSE" "$ver" | sort -V | head -n1)" != "$MIN_COMPOSE" ]; then
  echo "compose.sh: Docker Compose >= $MIN_COMPOSE required (found '${ver:-none}')" >&2
  exit 1
fi
exec docker compose -p catallax-demo --project-directory "$STACK" \
  -f docker-compose.yml -f deploy/demo/compose.demo.yml "$@"
