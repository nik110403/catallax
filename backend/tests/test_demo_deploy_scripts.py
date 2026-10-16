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
        "DEMO_MODE": "true", "DEMO_SAMPLE_READS": "true", "SHARE_PASSPHRASE": "", "DEMO_BIND_ADDR": LAN,
        "OPENBB_FRED_API_KEY": "", "DEMO_FRED_API_KEY": "",
    })
    return {
        "name": "catallax-demo",
        "networks": {"default": {"ipam": {"config": [{"subnet": "172.30.200.0/24"}]}},
                     "macro-net": {"ipam": {"config": [{"subnet": "172.30.201.0/24"}]}}},
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
            "demo-warm": _svc(networks={"default": None, "macro-net": None},
                              environment={"OPENBB_FRED_API_KEY": "demo-fred-key", "DEMO_FRED_API_KEY": ""}),
            "openbb-data": _svc(networks={"macro-net": None}, pids_limit=256, user="10001:10001", logging={"driver": "local", "options": {"max-size": "1m", "max-file": "1", "compress": "false"}},
                                environment={"OPENBB_FRED_API_KEY": "demo-fred-key", "OPENBB_API_PORT": "6900"}),
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


def test_preflight_allowlists_the_demo_seed_service(tmp_path: Path) -> None:
    render = _render()
    render["services"]["demo-seed"] = _svc(cap_add=[])
    assert _run_preflight(tmp_path, render).returncode == 0
    render["services"]["not-a-demo-service"] = _svc()
    assert _run_preflight(tmp_path, render).returncode != 0


def test_preflight_requires_demo_sample_reads_pinned(tmp_path: Path) -> None:
    render = _render()
    render["services"]["fastapi-backend"]["environment"]["DEMO_SAMPLE_READS"] = "false"
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "DEMO_SAMPLE_READS" in r.stderr


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
    ([{"subnet": "172.16.0.0/12"}], "/16 to /28"),
    ([{"subnet": "172.30.200.0/30"}], "/16 to /28"),
    ([{"subnet": "fd00::/64"}], "/16 to /28"),
    ([{"subnet": "172.30.200.0/24"}, {"subnet": "172.30.201.0/24"}], "exactly one"),
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


_OUTSIDE = {"type": "bind", "source": "/srv/catallax-handoff", "target": "/import", "read_only": True}


@pytest.mark.parametrize("name", ["demo-warm", "fastapi-backend", "redis", "openbb-data"])
@pytest.mark.parametrize("source", ["/srv/catallax-handoff", "/etc", "/", "/var/run", "/srv/../etc"])
def test_preflight_rejects_every_bind_outside_the_stack_dir(tmp_path: Path, name: str, source: str) -> None:
    render = _render()
    render["services"][name]["volumes"] = [{**_OUTSIDE, "source": source}]
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and "outside the stack dir" in r.stderr, (name, source)


def _fail(tmp_path: Path, mutate, needle: str) -> None:  # noqa: ANN001
    render = _render()
    mutate(render)
    r = _run_preflight(tmp_path, render)
    assert r.returncode != 0 and needle in r.stderr, r.stderr


def test_preflight_rejects_backend_on_macro_net(tmp_path: Path) -> None:
    _fail(tmp_path, lambda r: r["services"]["fastapi-backend"].update(networks={"default": None, "macro-net": None}),
          "macro-net")


@pytest.mark.parametrize("name", ["nginx", "redis"])
def test_preflight_rejects_other_services_on_macro_net(tmp_path: Path, name: str) -> None:
    _fail(tmp_path, lambda r: r["services"][name].update(networks={"macro-net": None}), "macro-net")


def test_preflight_rejects_openbb_data_on_the_default_network_or_with_a_port(tmp_path: Path) -> None:
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(networks={"default": None, "macro-net": None}),
          "macro-net only")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(ports=[{"host_ip": LAN, "target": 6900}]),
          "host port")


def test_preflight_requires_pids_limit_and_non_root_on_openbb_data(tmp_path: Path) -> None:
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].pop("pids_limit"), "pids_limit")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(pids_limit=0), "pids_limit")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].pop("logging"), "logging")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(logging={"driver": "json-file"}), "logging")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(logging={"driver": "local", "options": {"max-size": "1m", "max-file": "5"}}), "logging")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].pop("user"), "non-root")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(user="0:0"), "non-root")
    _fail(tmp_path, lambda r: r["services"]["openbb-data"].update(user="root"), "non-root")


def test_preflight_rejects_a_backend_fred_key(tmp_path: Path) -> None:
    _fail(tmp_path, lambda r: r["services"]["fastapi-backend"]["environment"].update(OPENBB_FRED_API_KEY="k"), "OPENBB_FRED_API_KEY")
    _fail(tmp_path, lambda r: r["services"]["fastapi-backend"]["environment"].update(DEMO_FRED_API_KEY="k"), "FRED key")
    _fail(tmp_path, lambda r: r["services"]["redis"]["environment"].update(DEMO_FRED_API_KEY="k"), "FRED key")
    _fail(tmp_path, lambda r: r["services"]["fastapi-backend"]["environment"].pop("OPENBB_FRED_API_KEY"), "OPENBB_FRED_API_KEY")


def test_preflight_rejects_backend_pointed_at_openbb_data(tmp_path: Path) -> None:
    _fail(tmp_path, lambda r: r["services"]["fastapi-backend"]["environment"].update(OPENBB_API_URL="http://openbb-data:6900"),
          "OPENBB_API_URL")


def test_preflight_macro_subnet_rules(tmp_path: Path) -> None:
    def subnet(v):  # noqa: ANN001, ANN202
        return lambda r: r["networks"]["macro-net"].update(ipam={"config": [{"subnet": v}]})
    _fail(tmp_path, subnet("172.30.200.128/25"), "overlap")
    _fail(tmp_path, subnet("not-a-cidr"), "macro-net")
    _fail(tmp_path, subnet("172.30.201.0/30"), "macro-net")
    _fail(tmp_path, subnet("198.51.100.0/24"), "DEMO_TRUSTED_PROXY")
    _fail(tmp_path, lambda r: r["networks"]["macro-net"].pop("ipam"), "macro-net")
    r = _run_preflight(tmp_path, _render())
    assert r.returncode == 0, r.stderr


def _reset_stack(tmp_path: Path, fail_on: str) -> tuple[Path, Path]:
    """A copy of reset-demo.sh next to stub compose.sh/preflight/docker that log every call."""
    stack = tmp_path / "stack"
    demo = stack / "deploy" / "demo"
    demo.mkdir(parents=True)
    (demo / "reset-demo.sh").write_text((DEMO / "reset-demo.sh").read_text())
    (stack / (".e" + "nv")).write_text("DEMO_DENY_MARKERS_FILE=/dev/null\n")
    calls = tmp_path / "calls.log"
    stubs = {
        "preflight-demo.sh": "#!/bin/sh\nexit 0\n",
        "compose.sh": '#!/bin/sh\necho "compose $*" >> "%s"\n'
                      '[ "$1" = "%s" ] && { echo "boom from $1" >&2; exit 1; }\n'
                      'case "$*" in *logs*) echo "LOGS-OF $*";; *exec*) echo 403;; *"run --rm demo-warm"*) exit ${WARM_RC:-0};; esac\nexit 0\n' % (calls, fail_on),
    }
    for name, body in stubs.items():
        (demo / name).write_text(body)
    for f in demo.iterdir():
        f.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / "docker"
    d.write_text("#!/bin/sh\n[ \"$2\" = inspect ] && exit 1\nexit 0\n")  # volumes are gone after rm
    d.chmod(0o755)
    return demo / "reset-demo.sh", bindir


def _run_reset(tmp_path: Path, fail_on: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    script, bindir = _reset_stack(tmp_path, fail_on)
    r = subprocess.run([str(script)], env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
                       capture_output=True, text=True)
    return r, (tmp_path / "calls.log").read_text().splitlines()


def test_reset_builds_the_images_before_tearing_the_stack_down(tmp_path: Path) -> None:
    # Regression (C7d): a stale catallax-demo-backend:local lacked app.demo_seed, so demo-seed exited 1.
    r, calls = _run_reset(tmp_path, fail_on="up")
    assert calls[0] == "compose build", calls
    assert calls.index("compose build") < calls.index("compose --profile warm down --remove-orphans")


def test_reset_build_failure_leaves_the_running_stack_alone(tmp_path: Path) -> None:
    r, calls = _run_reset(tmp_path, fail_on="build")
    assert r.returncode == 1
    assert calls == ["compose build"], calls


def test_reset_prints_one_shot_and_backend_logs_before_the_trap_takes_the_stack_down(tmp_path: Path) -> None:
    r, calls = _run_reset(tmp_path, fail_on="up")
    assert r.returncode == 1
    logs = [i for i, c in enumerate(calls) if " logs " in c]
    assert len(logs) == 4, calls
    for svc in ("demo-seed", "demo-warm", "notes-mount-init", "fastapi-backend"):
        assert f"LOGS-OF --profile warm logs --no-color --tail 80 {svc}" in r.stderr, svc
    assert calls[-1] == "compose --profile warm down --remove-orphans" and max(logs) == len(calls) - 2, calls


def test_reset_stops_the_macro_sidecar_after_the_warm_and_keeps_the_warm_status(tmp_path: Path) -> None:
    r, calls = _run_reset(tmp_path, fail_on="never")
    assert r.returncode == 0
    assert calls[-2:] == ["compose --profile warm run --rm demo-warm", "compose --profile warm rm -sf openbb-data"], calls


def test_reset_warm_failure_still_stops_the_sidecar_and_does_not_take_the_stack_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARM_RC", "3")
    r, calls = _run_reset(tmp_path, fail_on="never")
    assert r.returncode == 3
    assert calls[-1] == "compose --profile warm rm -sf openbb-data", calls
    assert calls.count("compose --profile warm down --remove-orphans") == 1, calls  # only the planned teardown; the stack stays up


def _warm_stack(tmp_path: Path, fail_warm: bool, preflight_fails: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
    demo = tmp_path / "stack" / "deploy" / "demo"
    demo.mkdir(parents=True)
    (demo / "warm-demo.sh").write_text((DEMO / "warm-demo.sh").read_text())
    calls = tmp_path / "calls.log"
    (demo / "preflight-demo.sh").write_text('#!/bin/sh\necho preflight >> "%s"\nexit %d\n' % (calls, 1 if preflight_fails else 0))
    (demo / "compose.sh").write_text('#!/bin/sh\necho "compose $*" >> "%s"\ncase "$*" in *"run --rm demo-warm"*) exit %d;; esac\nexit 0\n'
                                     % (calls, 3 if fail_warm else 0))
    for f in demo.iterdir():
        f.chmod(0o755)
    r = subprocess.run([str(demo / "warm-demo.sh")], capture_output=True, text=True)
    return r, calls.read_text().splitlines()


@pytest.mark.parametrize("fail,rc", [(False, 0), (True, 3)])
def test_warm_script_always_stops_the_sidecar_and_returns_the_warm_status(tmp_path: Path, fail: bool, rc: int) -> None:
    r, calls = _warm_stack(tmp_path, fail)
    assert r.returncode == rc, r.stderr
    assert calls == ["preflight", "compose --profile warm run --rm demo-warm", "compose --profile warm rm -sf openbb-data"]


def test_warm_script_runs_preflight_first_and_does_not_warm_on_a_failure(tmp_path: Path) -> None:
    r, calls = _warm_stack(tmp_path, fail_warm=False, preflight_fails=True)
    assert r.returncode != 0 and calls == ["preflight"], calls


@pytest.mark.parametrize("script", ["warm", "reset"])
def test_a_killed_script_still_stops_the_sidecar(tmp_path: Path, script: str) -> None:
    """SIGTERM to the process group while the warm runs (stub compose blocks) must still remove openbb-data."""
    import signal
    import time

    if script == "warm":
        _warm_stack(tmp_path, fail_warm=False)  # lays out the stack; re-run below with a blocking stub
        demo = tmp_path / "stack" / "deploy" / "demo"
        cmd, env = [str(demo / "warm-demo.sh")], os.environ
    else:
        sh, bindir = _reset_stack(tmp_path, fail_on="never")
        demo, cmd, env = sh.parent, [str(sh)], {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    calls = tmp_path / "calls.log"
    calls.write_text("")
    (demo / "compose.sh").write_text('#!/bin/sh\necho "compose $*" >> "%s"\ncase "$*" in *exec*) echo 403;; *"run --rm demo-warm"*) exec sleep 30;; esac\nexit 0\n' % calls)
    p = subprocess.Popen(cmd, env=env, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if "run --rm demo-warm" in calls.read_text():
            break
        time.sleep(0.1)
    os.killpg(p.pid, signal.SIGTERM)  # like systemd: the whole group, so the blocked child dies and the trap runs
    p.wait(timeout=20)
    assert p.returncode != 0
    assert calls.read_text().splitlines()[-1] == "compose --profile warm rm -sf openbb-data"
