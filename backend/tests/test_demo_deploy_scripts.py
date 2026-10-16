"""Deploy-script checks for the hosted demo: preflight (fixture render, no docker)
and the compose.sh version gate (fake `docker` on PATH)."""
from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "deploy" / "demo"
LAN = "198.51.100.7"  # TEST-NET-2: stands in for the operator's private LAN address
PROXY = "198.51.100.9/32"


def _svc(**kw: object) -> dict:
    base = {
        "mem_limit": "64m", "cpus": 0.5, "read_only": True, "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "environment": {"DEMO_BIND_ADDR": LAN, "DEMO_TRUSTED_PROXY": PROXY, "POSTGRES_DB": "x"},
    }
    base.update(kw)
    return base


def _render() -> dict:
    backend = _svc(environment={
        "DEMO_READONLY": "true", "UPSTREAM_MODE": "cache-only", "LLM_PROVIDER": "noop",
        "DEMO_MODE": "true", "SHARE_PASSPHRASE": "", "DEMO_BIND_ADDR": LAN,
    })
    return {
        "name": "catallax-demo",
        "services": {
            "nginx": _svc(
                cap_add=["CHOWN", "SETGID", "SETUID", "NET_BIND_SERVICE"],
                ports=[{"host_ip": LAN, "target": 80, "published": "8190"}],
            ),
            "fastapi-backend": backend,
            "redis": _svc(cap_add=["CHOWN", "SETGID", "SETUID"]),
        },
        "configs": {"demo_trusted_proxy": {"content": PROXY + " 1;"}},
    }


def _run_preflight(tmp_path: Path, render: dict) -> subprocess.CompletedProcess:
    markers = tmp_path / "markers.txt"
    markers.write_text(r"198\.51\.100\." + "\n")
    rf = tmp_path / "render.json"
    rf.write_text(json.dumps(render))
    env = {**os.environ, "PREFLIGHT_DEMO_RENDER": str(rf), "DEMO_DENY_MARKERS_FILE": str(markers),
           "DEMO_BIND_ADDR": LAN}
    return subprocess.run([str(DEMO / "preflight-demo.sh")], env=env, capture_output=True, text=True)


def test_preflight_ignores_bind_addr_and_proxy_keys_from_env_file(tmp_path: Path) -> None:
    r = _run_preflight(tmp_path, _render())
    assert r.returncode == 0, r.stderr


def test_preflight_still_trips_on_the_lan_address_under_another_key(tmp_path: Path) -> None:
    render = _render()
    render["services"]["redis"]["environment"]["SOME_OTHER_KEY"] = LAN
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "deny marker" in r.stderr


def test_preflight_trusted_proxy_must_be_narrow_cidr(tmp_path: Path) -> None:
    for content in ("0.0.0.0/0 1;", "198.51.100.0/4 1;", "not-a-cidr 1;"):
        render = _render()
        render["configs"]["demo_trusted_proxy"]["content"] = content
        r = _run_preflight(tmp_path, render)
        assert r.returncode != 0 and "demo_trusted_proxy" in r.stderr, content
    render = _render()
    del render["configs"]
    assert _run_preflight(tmp_path, render).returncode != 0


@pytest.mark.parametrize("opts", [
    ["no-new-privileges:true", "seccomp=unconfined"],
    ["no-new-privileges:true", "label:disable"],
    ["no-new-privileges:true", "label=disable"],
    ["no-new-privileges:true", "apparmor:foo"],
    [],
])
def test_preflight_security_opt_must_be_exactly_no_new_privileges(tmp_path: Path, opts: list[str]) -> None:
    render = copy.deepcopy(_render())
    render["services"]["redis"]["security_opt"] = opts
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "security_opt" in r.stderr


def _fake_docker(tmp_path: Path, version: str) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / "docker"
    d.write_text('#!/bin/sh\nif [ "$2" = version ]; then echo "%s"; else echo RAN; fi\n' % version)
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


@pytest.mark.parametrize("version,ok", [
    ("2.24.4", True), ("v2.29.1-desktop.1", True), ("2.30.0", True), ("3.0.0", True),
    ("2.24.3", False), ("2.23.9", False), ("2.9.0", False), ("", False),
])
def test_compose_sh_version_gate_fails_closed(tmp_path: Path, version: str, ok: bool) -> None:
    r = subprocess.run([str(DEMO / "compose.sh"), "config"], env=_fake_docker(tmp_path, version),
                       capture_output=True, text=True)
    if ok:
        assert r.returncode == 0 and "RAN" in r.stdout, r.stderr
    else:
        assert r.returncode == 1 and "RAN" not in r.stdout and "2.24.4" in r.stderr
