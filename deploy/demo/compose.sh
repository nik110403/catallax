#!/usr/bin/env bash
# Pinned compose wrapper for the demo stack: fixed project name and files, so
# systemd units and operators cannot drift from the preflighted configuration.
#   deploy/demo/compose.sh up -d --wait
#   deploy/demo/compose.sh --profile warm run --rm demo-warm
set -euo pipefail
STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$STACK"
exec docker compose -p catallax-demo --project-directory "$STACK" \
  -f docker-compose.yml -f deploy/demo/compose.demo.yml "$@"
