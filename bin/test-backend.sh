#!/usr/bin/env bash
# Full backend suite with no env file (tests/conftest.py supplies the env defaults).
# DB-gated suites skip when Postgres is unreachable.
set -euo pipefail
cd "$(dirname "$0")/../backend"
.venv/bin/python -m pytest -q -p no:cacheprovider "$@" 2>&1 | tail -15
