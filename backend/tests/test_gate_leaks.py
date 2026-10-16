"""Regression tests for two Phase-14 SEV3 s closed the same
day: `/api/v1/session` leaking `owner_name` pre-login, and `/api/v1/ws/prices`
leaking the owner's persisted watchlist to a bare unauthenticated connect.
Both routes sit outside the share gate by design (session must be readable
pre-login; the websocket scope bypasses `ShareGateMiddleware` entirely), so
each route is responsible for not leaking owner data on its own.

Same hand-rolled style as `test_share_gate.py`: fabricated `Request`/fake
websocket objects, no real DB/Redis/network, no shared test harness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Request, WebSocketDisconnect

import app.routers.ws as ws_module
from app.adapters.base import Quote
from app.auth import COOKIE_NAME, _cookie_value
from app.config import Settings
from app.routers.session import session as session_endpoint

_PASSPHRASE = "correct-horse-battery-staple"  # >= 12 chars (config.py's floor)


def _settings(**kw: object) -> Settings:
    kw.setdefault("share_passphrase", "")
    # Pin explicitly: Settings has no model_config isolating it from the
    # process env, so an exported DEMO_MODE=true (e.g. a parallel dev shell)
    # would otherwise make the two "returns owner_name" tests below fail
    # spuriously — demo mode blanks owner_name independently of auth.
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _gated_settings(**kw: object) -> Settings:
    kw.setdefault("share_passphrase", _PASSPHRASE)
    return _settings(**kw)


def _http_scope(path: str, *, cookie: str | None = None) -> dict:
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }


# ── /api/v1/session — owner_name pre-login leak ────────────────
async def test_session_withholds_owner_name_when_unauthenticated_and_gated() -> None:
    settings = _gated_settings()
    request = Request(_http_scope("/api/v1/session"))  # no cookie
    info = await session_endpoint(request, settings)
    assert info.authenticated is False
    assert info.auth_required is True
    assert info.owner_name is None


async def test_session_returns_owner_name_when_authenticated_real_mode() -> None:
    settings = _gated_settings()
    cookie_val = _cookie_value(settings, remember=True)
    request = Request(_http_scope("/api/v1/session", cookie=cookie_val))
    info = await session_endpoint(request, settings)
    assert info.authenticated is True
    assert info.owner_name == settings.owner_name


async def test_session_returns_owner_name_when_no_passphrase_configured() -> None:
    settings = _settings()  # empty passphrase -> authenticated is always True
    request = Request(_http_scope("/api/v1/session"))  # no cookie needed
    info = await session_endpoint(request, settings)
    assert info.auth_required is False
    assert info.authenticated is True
    assert info.owner_name == settings.owner_name


async def test_session_withholds_owner_name_in_demo_mode_even_when_authenticated() -> None:
    settings = _gated_settings(demo_mode=True)
    cookie_val = _cookie_value(settings, remember=True)
    request = Request(_http_scope("/api/v1/session", cookie=cookie_val))
    info = await session_endpoint(request, settings)
    assert info.authenticated is True  # demo_mode is a separate, additional block
    assert info.demo_mode is True
    assert info.owner_name is None


# ── /api/v1/ws/prices — unauthenticated watchlist leak ─────────
class _FakeWebSocket:
    """Stand-in for `starlette.websockets.WebSocket`: records sent frames and
    whether `close()` was called. `disconnect_after` simulates the client
    hanging up after N frames by raising `WebSocketDisconnect` from
    `send_json`, mirroring how the real socket would end the streaming loop."""

    def __init__(
        self,
        query_params: dict,
        *,
        disconnect_after: int | None = None,
        cookies: dict | None = None,
    ) -> None:
        self.query_params = query_params
        self.cookies = cookies or {}
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self.accept_called = False
        self._disconnect_after = disconnect_after

    async def accept(self) -> None:
        self.accept_called = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        if self._disconnect_after is not None and len(self.sent) >= self._disconnect_after:
            raise WebSocketDisconnect()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True
        self.close_code = code


async def test_ws_prices_without_symbols_sends_no_symbols_error_and_closes(monkeypatch) -> None:
    """FIX: `?symbols=` is now required. Regression target — a reinstated
    watchlist fallback would either populate `tick`/`prices` in the sent
    frame or never call `close()`; both are asserted against directly. As a
    belt-and-suspenders check (no DB/watchlist row is seeded in this
    pure-unit harness — see task note), the now-deleted helper is also
    confirmed gone from the module."""
    settings = _settings()
    monkeypatch.setattr(ws_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ws_module, "get_redis", lambda: object())

    fake = _FakeWebSocket({})  # no "symbols" key at all
    await ws_module.ws_prices(fake)

    assert fake.sent == [
        {"error": {"code": "NO_SYMBOLS", "message": "No symbols to stream"}}
    ]
    assert fake.closed is True
    assert not hasattr(ws_module, "_watchlist_symbols")
    assert not hasattr(ws_module, "session_scope")  # the deleted DB import stays gone


async def test_ws_prices_with_symbols_still_streams(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(ws_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ws_module, "get_redis", lambda: object())

    quote = Quote(
        symbol="AAPL",
        price=Decimal("100"),
        currency="USD",
        as_of=datetime.now(timezone.utc),
        source="yfinance",
    )

    class _FakeAdapter:
        async def get_quote(self, symbol: str) -> Quote:
            return quote

    monkeypatch.setattr(ws_module, "build_adapter", lambda source, redis, settings: _FakeAdapter())

    fake = _FakeWebSocket({"symbols": "AAPL"}, disconnect_after=1)
    await ws_module.ws_prices(fake)

    assert fake.sent[0]["prices"]["AAPL"]["price"] == "100"
    assert fake.sent[0]["prices"]["AAPL"]["source"] == "yfinance"


# ── /api/v1/ws/prices — unauthenticated + unbounded (security review 2026-08-15) ──
async def test_ws_prices_rejects_unauthenticated_before_accept(monkeypatch) -> None:
    """`ShareGateMiddleware` passes non-"http" scopes straight through, so the
    WS route authenticates itself before `accept()` once a passphrase is set."""
    settings = _gated_settings()
    monkeypatch.setattr(ws_module, "get_settings", lambda: settings)

    fake = _FakeWebSocket({"symbols": "AAPL"})  # no cookie
    await ws_module.ws_prices(fake)

    assert fake.accept_called is False
    assert fake.closed is True
    assert fake.close_code == 4401
    assert fake.sent == []


async def test_ws_prices_accepts_authenticated_when_gated(monkeypatch) -> None:
    settings = _gated_settings()
    monkeypatch.setattr(ws_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ws_module, "get_redis", lambda: object())

    quote = Quote(
        symbol="AAPL",
        price=Decimal("100"),
        currency="USD",
        as_of=datetime.now(timezone.utc),
        source="yfinance",
    )

    class _FakeAdapter:
        async def get_quote(self, symbol: str) -> Quote:
            return quote

    monkeypatch.setattr(ws_module, "build_adapter", lambda source, redis, settings: _FakeAdapter())

    cookie_val = _cookie_value(settings, remember=True)
    fake = _FakeWebSocket(
        {"symbols": "AAPL"}, disconnect_after=1, cookies={COOKIE_NAME: cookie_val}
    )
    await ws_module.ws_prices(fake)

    assert fake.accept_called is True
    assert fake.sent[0]["prices"]["AAPL"]["price"] == "100"


def test_parse_query_symbols_drops_bad_charset_symbols() -> None:
    out = ws_module._parse_query_symbols(
        "AAPL,../../V7/FINANCE,^GSPC,EURUSD=X,GC=F,BRK-B,bad$sym"
    )
    assert out == [
        ("AAPL", None),
        ("^GSPC", None),
        ("EURUSD=X", None),
        ("GC=F", None),
        ("BRK-B", None),
    ]


def test_parse_query_symbols_drops_pure_punctuation_and_dotdot() -> None:
    # Security review (2026-08-15): "..", "^^", "==" and "--" are all within
    # the allowed charset but carry no alnum — ".." reaches yfinance's
    # upstream URL path unchanged (/v8/finance/chart/..) if let through.
    out = ws_module._parse_query_symbols("..,^^,==,--,AAPL")
    assert out == [("AAPL", None)]
