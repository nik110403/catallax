#!/usr/bin/env bash
# Preflight for the hosted demo stack (DEMO-SPEC §4). Renders the merged compose
# config and FAILS CLOSED on anything that could reach the private stack or open
# a write/upstream path. Run before the first `up` and after every config change.
#   deploy/demo/preflight-demo.sh
# Private-infrastructure deny markers (host names, internal ranges, network names)
# are NOT kept in this repo. DEMO_DENY_MARKERS_FILE names a file outside it, one
# Python regex per line ('#' comments and blank lines ignored). Unset, missing or
# empty => the preflight fails closed. It may also be set in the stack .env.
# DEMO_BIND_ADDR (stack .env, default 127.0.0.1) is the one allowed port host_ip.
# The env_file copy of it that compose inlines into each service's environment is
# ignored by key name before the marker scan (every other key is still scanned).
# Docker Compose >= 2.24.4 is required (compose.sh checks and fails closed).
# Test hook: PREFLIGHT_DEMO_RENDER=<file.json> checks a saved `config --format json`
# render instead of invoking docker.
set -euo pipefail

STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
fail() { echo "PREFLIGHT-DEMO FAIL: $*" >&2; exit 1; }
command -v python3 >/dev/null || fail "python3 not found"

# Stack .env value for KEY (last assignment wins); shell env wins for the marker file.
env_val() { sed -n "s/^$1=//p" "$STACK/.env" 2>/dev/null | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"; }
DEMO_DENY_MARKERS_FILE="${DEMO_DENY_MARKERS_FILE:-$(env_val DEMO_DENY_MARKERS_FILE)}"
[ -n "$DEMO_DENY_MARKERS_FILE" ] || fail "DEMO_DENY_MARKERS_FILE is not set (file of private deny markers, outside the repo)"
[ -r "$DEMO_DENY_MARKERS_FILE" ] || fail "DEMO_DENY_MARKERS_FILE $DEMO_DENY_MARKERS_FILE is not readable"
DEMO_BIND_ADDR="${DEMO_BIND_ADDR:-$(env_val DEMO_BIND_ADDR)}"; DEMO_BIND_ADDR="${DEMO_BIND_ADDR:-127.0.0.1}"
export DEMO_DENY_MARKERS_FILE DEMO_BIND_ADDR

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

# Capabilities some images genuinely need (entrypoint chown / setuid / port 80).
# Anything not listed here must not add capabilities.
CAP_ADD_ALLOWED = {
    "nginx": {"CHOWN", "SETGID", "SETUID", "NET_BIND_SERVICE"},
    "react-frontend": {"CHOWN", "SETGID", "SETUID", "NET_BIND_SERVICE"},
    "postgres": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
    "redis": {"CHOWN", "SETGID", "SETUID"},
    "notes-mount-init": {"DAC_OVERRIDE"},  # mkdir on a root-owned volume
}
# Services that may keep a writable root filesystem (none: writable paths are
# tmpfs or volumes). Add "service": "reason" here if one truly needs it.
WRITABLE_ROOTFS_ALLOWED = {}

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
    # Exactly this and nothing else: unconfined, label:disable, apparmor/seccomp
    # overrides and anything yet to be invented are all refused.
    if sorted(str(o) for o in (svc.get("security_opt") or [])) != ["no-new-privileges:true"]:
        bad.append(L("security_opt must be exactly [no-new-privileges:true], got %r" % (svc.get("security_opt"),)))
    if "ALL" not in [str(c).upper() for c in (svc.get("cap_drop") or [])]:
        bad.append(L("missing cap_drop: [ALL]"))
    if svc.get("read_only") is not True and sname not in WRITABLE_ROOTFS_ALLOWED:
        bad.append(L("missing read_only: true"))
    extra_caps = {str(c).upper() for c in (svc.get("cap_add") or [])} - CAP_ADD_ALLOWED.get(sname, set())
    if extra_caps:
        bad.append(L("cap_add %s not allowlisted" % sorted(extra_caps)))
    for n in (svc.get("networks") or {}):
        if n not in (cfg.get("networks") or {}):
            bad.append(L("network %r is not declared by this stack" % n))

# Required demo environment on the backend.
be = (services.get("fastapi-backend") or {}).get("environment") or {}
want = {"DEMO_READONLY": "true", "UPSTREAM_MODE": "cache-only", "LLM_PROVIDER": "noop",
        "DEMO_MODE": "true"}
for k, v in want.items():
    if str(be.get(k, "")).lower() != v:
        bad.append("fastapi-backend: %s=%r, expected %r" % (k, be.get(k), v))
if str(be.get("SHARE_PASSPHRASE", "x")) != "":
    bad.append("fastapi-backend: SHARE_PASSPHRASE must be empty")

# Private-infrastructure markers anywhere in the render. Port host_ip is the
# operators chosen LAN bind address and is excluded from the scan (it must equal
# DEMO_BIND_ADDR; wildcard binds were rejected above).
bind = os.environ["DEMO_BIND_ADDR"]
scan = json.loads(json.dumps(cfg))
for svc in (scan.get("services") or {}).values():
    for pt in svc.get("ports") or []:
        if not isinstance(pt, dict) or not pt.get("host_ip"):
            continue
        if pt["host_ip"] != bind:
            bad.append("port host_ip %r is not DEMO_BIND_ADDR (%r)" % (pt["host_ip"], bind))
        pt.pop("host_ip")
TP_ENV = {"DEMO_BIND_ADDR", "DEMO_TRUSTED_PROXY"}
# DEMO_BIND_ADDR itself reaches the services through env_file, where the
# render inlines it into `environment`. Drop that KEY (by name only) before the
# scan: any other key that carries the LAN address still trips the markers.
for svc in (scan.get("services") or {}).values():
    env = svc.get("environment")
    if isinstance(env, dict):
        for k in TP_ENV:
            env.pop(k, None)
    elif isinstance(env, list):
        svc["environment"] = [e for e in env if str(e).split("=", 1)[0] not in TP_ENV]
# DEMO_TRUSTED_PROXY (the edge proxy CIDR) is private by nature: it reaches the render
# via env_file and via the nginx `configs` content. Validate its shape, then exempt
# exactly those carriers from the scan.
tp = (scan.get("configs") or {}).get("demo_trusted_proxy")
if tp is None:
    bad.append("config demo_trusted_proxy missing (nginx would have no trusted-proxy set)")
else:
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))? 1;\n?", str(tp.get("content", "")))
    if not m or any(int(o) > 255 for o in m.group(1).split(".")) or (m.group(2) and int(m.group(2)) < 8):
        bad.append("config demo_trusted_proxy is not a single IPv4 CIDR no broader than /8")
    tp.pop("content", None)
blob = json.dumps(scan)
markers = []
with open(os.environ["DEMO_DENY_MARKERS_FILE"]) as fh:
    for line in fh:
        line = line.strip()
        if line and not line.startswith("#"):
            markers.append(line)
if not markers:
    bad.append("deny-marker file has no markers (fail closed)")
for pat in markers:
    if re.search(pat, blob):
        bad.append("rendered config matches a private deny marker (see the marker file)")

if bad:
    sys.exit("\n".join("  - " + b for b in bad))
' || fail "violations (above)"

echo "PREFLIGHT-DEMO PASS (stack dir: $STACK)"
