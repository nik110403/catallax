"""DemoReadOnlyMiddleware (hosted-demo write guard, DEMO-SPEC §2.2): with
DEMO_READONLY on, every non-GET/HEAD/OPTIONS /api/v1/* route in the real app's
route table answers 403 {"error":"read_only_demo"}; flag off changes nothing.
Pure ASGI scopes, no network/DB/Redis (same style as test_share_gate.py)."""
from __future__ import annotations

import json
import re

import pytest

import app.auth as auth_module
from app.auth import DemoReadOnlyMiddleware
from app.config import Settings
from app.main import create_app

_SAFE = {"GET", "HEAD", "OPTIONS"}


def _settings(**kw: object) -> Settings:
    kw.setdefault("share_passphrase", "")
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _scope(method: str, path: str, scope_type: str = "http") -> dict:
    return {
        "type": scope_type, "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
        "server": ("testserver", 80), "scheme": "http",
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class _Inner:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _Rec:
    def __init__(self) -> None:
        self.msgs: list[dict] = []

    async def __call__(self, m: dict) -> None:
        self.msgs.append(m)

    @property
    def status(self) -> int | None:
        return next((m["status"] for m in self.msgs if m["type"] == "http.response.start"), None)

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.msgs if m["type"] == "http.response.body")


def _write_routes() -> list[tuple[str, str]]:
    """(method, concrete path) for every non-safe /api/v1 route. Read from the
    app's OpenAPI route table (FastAPI wraps included routers, so `app.routes`
    is not flat); the schema covers every HTTP route, so routes added later are
    picked up automatically."""
    out: list[tuple[str, str]] = []
    for path, ops in create_app().openapi()["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for m in sorted(ops):
            if m.upper() not in _SAFE:
                out.append((m.upper(), re.sub(r"\{[^}]+\}", "x", path)))
    return out


def test_route_table_has_write_routes() -> None:
    # Guards the enumeration itself: spec counts 26 POST (+2 auth), 2 PUT, 2 PATCH, 2 DELETE.
    routes = _write_routes()
    assert len(routes) >= 34
    assert {m for m, _ in routes} >= {"POST", "PUT", "PATCH", "DELETE"}


async def test_every_write_route_is_403_when_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_module, "get_settings", lambda: _settings(demo_readonly=True))
    app = create_app()  # the real stack, so the middleware wiring is covered too
    for method, path in _write_routes():
        rec = _Rec()
        await app(_scope(method, path), _receive, rec)
        assert rec.status == 403, (method, path)
        assert json.loads(rec.body) == {"error": "read_only_demo"}, (method, path)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_safe_methods_pass_when_readonly(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(auth_module, "get_settings", lambda: _settings(demo_readonly=True))
    inner, rec = _Inner(), _Rec()
    await DemoReadOnlyMiddleware(inner)(_scope(method, "/api/v1/anything"), _receive, rec)
    assert inner.called and rec.status == 200


async def test_non_api_paths_and_slash_tricks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_module, "get_settings", lambda: _settings(demo_readonly=True))
    inner, rec = _Inner(), _Rec()
    await DemoReadOnlyMiddleware(inner)(_scope("POST", "/not-api"), _receive, rec)
    assert inner.called  # outside /api/v1: not this middleware's business (edge covers it)
    inner2, rec2 = _Inner(), _Rec()
    await DemoReadOnlyMiddleware(inner2)(_scope("POST", "//api//v1/x"), _receive, rec2)
    assert rec2.status == 403 and not inner2.called


async def test_websocket_scope_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_module, "get_settings", lambda: _settings(demo_readonly=True))
    inner = _Inner()
    await DemoReadOnlyMiddleware(inner)(_scope("GET", "/api/v1/ws/prices", "websocket"), _receive, _Rec())
    assert inner.called


async def test_flag_off_passes_every_write_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_module, "get_settings", lambda: _settings(demo_readonly=False))
    for method, path in _write_routes():
        inner, rec = _Inner(), _Rec()
        await DemoReadOnlyMiddleware(inner)(_scope(method, path), _receive, rec)
        assert inner.called and rec.status == 200, (method, path)


async def test_fails_closed_on_settings_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(auth_module, "get_settings", _boom)
    inner, rec = _Inner(), _Rec()
    await DemoReadOnlyMiddleware(inner)(_scope("POST", "/api/v1/x"), _receive, rec)
    assert rec.status == 403 and not inner.called
