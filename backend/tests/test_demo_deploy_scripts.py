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
        "networks": {"default": {"ipam": {"config": [{"subnet": "10.200.0.0/24"}]}}},
        "services": {
            "nginx": _svc(
                cap_add=["CHOWN", "SETGID", "SETUID", "NET_BIND_SERVICE"],
                ports=[{"host_ip": LAN, "target": 80, "published": "8190"}],
                volumes=[{"type": "bind", "source": str(DEMO / "nginx-templates"),
                          "target": "/etc/nginx/templates", "read_only": True}],
                environment={"DEMO_BIND_ADDR": LAN, "DEMO_TRUSTED_PROXY": PROXY,
                             "NGINX_ENVSUBST_OUTPUT_DIR": "/etc/nginx/demo-env"},
                tmpfs=["/var/cache/nginx", "/etc/nginx/demo-env:mode=0755,noexec,nosuid"],
            ),
            "fastapi-backend": backend,
            "redis": _svc(cap_add=["CHOWN", "SETGID", "SETUID"]),
        },
    }


def _run_preflight(tmp_path: Path, render: dict, script: Path = DEMO / "preflight-demo.sh") -> subprocess.CompletedProcess:
    markers = tmp_path / "markers.txt"
    markers.write_text(r"198\.51\.100\." + "\n")
    rf = tmp_path / "render.json"
    rf.write_text(json.dumps(render))
    env = {**os.environ, "PREFLIGHT_DEMO_RENDER": str(rf), "DEMO_DENY_MARKERS_FILE": str(markers),
           "DEMO_BIND_ADDR": LAN}
    return subprocess.run([str(script)], env=env, capture_output=True, text=True)


def test_preflight_ignores_bind_addr_and_proxy_keys_from_env_file(tmp_path: Path) -> None:
    r = _run_preflight(tmp_path, _render())
    assert r.returncode == 0, r.stderr


def test_preflight_still_trips_on_the_lan_address_under_another_key(tmp_path: Path) -> None:
    render = _render()
    render["services"]["redis"]["environment"]["SOME_OTHER_KEY"] = LAN
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "deny marker" in r.stderr


def test_preflight_trusted_proxy_must_be_narrow_cidr(tmp_path: Path) -> None:
    for value in ("0.0.0.0/0", "198.51.100.0/4", "not-a-cidr", "198.51.100.9/32 1;", ""):
        render = _render()
        render["services"]["nginx"]["environment"]["DEMO_TRUSTED_PROXY"] = value
        r = _run_preflight(tmp_path, render)
        assert r.returncode != 0 and "DEMO_TRUSTED_PROXY" in r.stderr, value
    render = _render()
    del render["services"]["nginx"]["environment"]["DEMO_TRUSTED_PROXY"]
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "DEMO_TRUSTED_PROXY" in r.stderr


def test_preflight_requires_template_mount_and_output_dir(tmp_path: Path) -> None:
    render = _render()
    render["services"]["nginx"]["volumes"] = []
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "template" in r.stderr
    render = _render()
    del render["services"]["nginx"]["environment"]["NGINX_ENVSUBST_OUTPUT_DIR"]
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "NGINX_ENVSUBST_OUTPUT_DIR" in r.stderr


def _copy_stack(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway stack (script + templates) so the template dir can be tampered with."""
    d = tmp_path / "stack" / "deploy" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "preflight-demo.sh").write_bytes((DEMO / "preflight-demo.sh").read_bytes())
    (d / "preflight-demo.sh").chmod(0o755)
    (d / "nginx-templates").mkdir(exist_ok=True)
    (d / "nginx-templates" / "demo-trusted-proxy.conf.template").write_bytes(
        (DEMO / "nginx-templates" / "demo-trusted-proxy.conf.template").read_bytes())
    return d, d / "preflight-demo.sh"


def _tampered(tmp_path: Path, mutate) -> subprocess.CompletedProcess:
    d, script = _copy_stack(tmp_path)
    mutate(d / "nginx-templates")
    render = _render()
    render["services"]["nginx"]["volumes"][0]["source"] = str(d / "nginx-templates")
    return _run_preflight(tmp_path, render, script)


def test_preflight_passes_on_a_copied_stack(tmp_path: Path) -> None:
    r = _tampered(tmp_path, lambda t: None)
    assert r.returncode == 0, r.stderr


def test_preflight_template_content_must_be_exactly_the_trust_line(tmp_path: Path) -> None:
    r = _tampered(tmp_path, lambda t: (t / "demo-trusted-proxy.conf.template").write_text("0.0.0.0/0 1;\n"))
    assert r.returncode != 0 and "demo-trusted-proxy.conf.template" in r.stderr
    (tmp_path / "stack").rename(tmp_path / "stack1")
    r = _tampered(tmp_path, lambda t: (t / "demo-trusted-proxy.conf.template").write_text(
        "# c\n\n${DEMO_TRUSTED_PROXY} 1;\n0.0.0.0/0 1;\n"))
    assert r.returncode != 0 and "demo-trusted-proxy.conf.template" in r.stderr


def test_preflight_template_dir_must_hold_one_file(tmp_path: Path) -> None:
    r = _tampered(tmp_path, lambda t: (t / "extra.template").write_text("x\n"))
    assert r.returncode != 0 and "exactly demo-trusted-proxy.conf.template" in r.stderr


def test_preflight_template_mount_source_and_mode(tmp_path: Path) -> None:
    render = _render()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    render["services"]["nginx"]["volumes"][0]["source"] = str(elsewhere)
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "template mount source" in r.stderr
    render = _render()
    render["services"]["nginx"]["volumes"][0]["read_only"] = False
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "read_only" in r.stderr


def test_preflight_requires_demo_env_tmpfs(tmp_path: Path) -> None:
    render = _render()
    render["services"]["nginx"]["tmpfs"] = ["/var/cache/nginx"]
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "demo-env" in r.stderr


@pytest.mark.parametrize("config,msg", [
    ([{"subnet": "198.51.100.0/24"}], "overlaps"),
    ([{"subnet": "10.0.0.0/8"}], "/16 to /28"),
    ([{"subnet": "10.200.0.0/30"}], "/16 to /28"),
    ([{"subnet": "fd00::/64"}], "/16 to /28"),
    ([{"subnet": "10.200.0.0/24"}, {"subnet": "10.201.0.0/24"}], "exactly one"),
    ([], "exactly one"),
])
def test_preflight_network_subnet_rules(tmp_path: Path, config: list, msg: str) -> None:
    render = _render()
    render["networks"]["default"]["ipam"]["config"] = config
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and msg in r.stderr


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
