#!/usr/bin/env bash
# Preflight for the hosted demo stack (DEMO-SPEC §4). Renders the merged compose
# config and FAILS CLOSED on anything that could reach the private stack or open
# a write/upstream path. Run before the first `up` and after every config change.
#   deploy/demo/preflight-demo.sh
# Test hook: PREFLIGHT_DEMO_RENDER=<file.json> checks a saved `config --format json`
# render instead of invoking docker.
set -euo pipefail

STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
fail() { echo "PREFLIGHT-DEMO FAIL: $*" >&2; exit 1; }
command -v python3 >/dev/null || fail "python3 not found"

if [ -n "${PREFLIGHT_DEMO_RENDER:-}" ]; then
  cfg="$(cat "$PREFLIGHT_DEMO_RENDER")"
else
  command -v docker >/dev/null || fail "docker CLI not found"
  # `--profile warm` renders the on-demand warm job too; the heavy profiles
  # (macro, lab, backtest, algo, never) stay un-rendered, so a service that
  # sneaks into the default set shows up in the allowlist check below.
  cfg="$("$STACK/deploy/demo/compose.sh" --profile warm config --format json)" \
    || fail "docker compose config failed"
fi

printf '%s' "$cfg" | STACK="$STACK" python3 -I -c '
import json, os, re, sys

cfg = json.load(sys.stdin)
stack = os.path.realpath(os.environ["STACK"])
bad = []

if cfg.get("name") != "catallax-demo":
    bad.append("project name is %r, expected catallax-demo" % cfg.get("name"))

# Own networks/volumes only.
for kind in ("networks", "volumes"):
    for n, v in (cfg.get(kind) or {}).items():
        v = v or {}
        if v.get("external"):
            bad.append("%s %r is external" % (kind[:-1], n))
        if v.get("driver_opts"):
            bad.append("%s %r has driver_opts" % (kind[:-1], n))
for kind in ("secrets", "configs"):
    for n, v in (cfg.get(kind) or {}).items():
        if (v or {}).get("external"):
            bad.append("%s %r is external" % (kind, n))

ALLOWED = {"nginx", "react-frontend", "fastapi-backend", "stub-exporter", "postgres",
           "redis", "fava", "notes-mount-init", "demo-warm"}
services = cfg.get("services") or {}
for sname in services:
    if sname not in ALLOWED:
        bad.append("service %r is not part of the demo allowlist" % sname)

def inside(p):
    rp = os.path.realpath(p)
    return rp == stack or rp.startswith(stack + os.sep)

for sname, svc in services.items():
    L = lambda s, sname=sname: "%s: %s" % (sname, s)
    if svc.get("privileged"):
        bad.append(L("privileged"))
    for k in ("pid", "ipc", "uts", "cgroup"):
        if svc.get(k) == "host":
            bad.append(L("%s: host" % k))
    nm = svc.get("network_mode")
    if nm:
        bad.append(L("network_mode %r" % nm))
    for k in ("devices", "volumes_from"):
        if svc.get(k):
            bad.append(L("sets %s" % k))
    for v in svc.get("volumes") or []:
        if not isinstance(v, dict):
            bad.append(L("volume entry not normalised: %r" % (v,)))
            continue
        src = str(v.get("source", ""))
        if "docker.sock" in src or "docker.sock" in str(v.get("target", "")):
            bad.append(L("mounts a docker socket"))
        if v.get("type") == "bind" and not inside(src):
            bad.append(L("bind mount %r is outside the stack dir" % src))
    for ef in svc.get("env_file") or []:
        p = ef.get("path") if isinstance(ef, dict) else ef
        if not p or not inside(p):
            bad.append(L("env_file %r outside the stack dir" % p))
    b = svc.get("build")
    if isinstance(b, dict) and b.get("context") and not inside(b["context"]):
        bad.append(L("build context %r outside the stack dir" % b["context"]))
    for pt in svc.get("ports") or []:
        hip = pt.get("host_ip") if isinstance(pt, dict) else None
        if hip in (None, "", "0.0.0.0", "::"):
            bad.append(L("publishes %r on all interfaces (set DEMO_BIND_ADDR to the LAN address)" % (pt,)))
    # Hardening every service must carry.
    if not svc.get("mem_limit"):
        bad.append(L("no mem_limit"))
    if not svc.get("cpus"):
        bad.append(L("no cpus limit"))
    if "no-new-privileges:true" not in (svc.get("security_opt") or []):
        bad.append(L("missing security_opt no-new-privileges:true"))
    if "ALL" not in [str(c).upper() for c in (svc.get("cap_drop") or [])]:
        bad.append(L("missing cap_drop: [ALL]"))
    if "wf-link" in (svc.get("networks") or {}):
        bad.append(L("on wf-link"))

# Required demo environment on the backend.
be = (services.get("fastapi-backend") or {}).get("environment") or {}
want = {"DEMO_READONLY": "true", "UPSTREAM_MODE": "cache-only", "LLM_PROVIDER": "noop",
        "DEMO_MODE": "true"}
for k, v in want.items():
    if str(be.get(k, "")).lower() != v:
        bad.append("fastapi-backend: %s=%r, expected %r" % (k, be.get(k), v))
if str(be.get("SHARE_PASSPHRASE", "x")) != "":
    bad.append("fastapi-backend: SHARE_PASSPHRASE must be empty")

# Private-infrastructure markers anywhere in the render.
blob = json.dumps(cfg)
for pat, label in ((r"home\.arpa", "home.arpa"), (r"(?<![0-9])10\.0\.0\.", "10.0.0."),
                   (r"core-01", "core-01"), (r"wf-link", "wf-link")):
    if re.search(pat, blob):
        bad.append("rendered config contains private marker %r" % label)

if bad:
    sys.exit("\n".join("  - " + b for b in bad))
' || fail "violations (above)"

echo "PREFLIGHT-DEMO PASS (stack dir: $STACK)"
