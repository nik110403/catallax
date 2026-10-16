"""Regression tests for the Phase-14a share gate (`app/auth.py`): the signed
session cookie, `ShareGateMiddleware`'s pure-ASGI enforcement, the login rate
limiter, and the fail-closed exception contract. Pure unit tests — fabricated
ASGI scope/receive/send and a hand-rolled fake Redis, no real network/DB/Redis
touched (mirrors `test_fallback.py`/`test_broker_factory.py`'s fake-adapter
style: small `_Fake*` stand-ins, no `unittest.mock`).
"""
from __future__ import annotations

import hmac
import time

import pytest
from fastapi import Request

import app.auth as auth_module
from app.auth import (
    _LOGIN_MAX_ATTEMPTS,
    _SHORT_SESSION_SECONDS,
    COOKIE_NAME,
    LoginRequest,
    ShareGateMiddleware,
    Unauthenticated,
    _client_ip,
    _collapse_slashes,
    _cookie_key,
    _cookie_value,
    _has_dot_segment,
    _login_fail_count,
    _register_login_failure,
    _sign,
    _verify_cookie,
    is_authenticated,
    login,
)
from app.config import Settings
from app.errors import RateLimitExceeded

_PASSPHRASE = "correct-horse-battery-staple"  # >= 12 chars (config.py's floor)


def _settings(**kw: object) -> Settings:
    # share_passphrase always explicit (never inherited from whatever real
    # SHARE_PASSPHRASE the container this suite runs in happens to have) so
    # "gate disabled" tests can never accidentally pick up a real secret.
    kw.setdefault("share_passphrase", "")
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _gated_settings(**kw: object) -> Settings:
    kw.setdefault("share_passphrase", _PASSPHRASE)
    return _settings(**kw)


def _http_scope(
    path: str,
    *,
    cookie: str | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 12345),
) -> dict:
    hdrs = list(headers or [])
    if cookie is not None:
        hdrs.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": hdrs,
        "client": client,
        "server": ("testserver", 80),
        "scheme": "http",
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class _InnerApp:
    """Stand-in for the wrapped FastAPI app: records whether it was reached."""

    def __init__(self) -> None:
        self.called = False
        self.received_scope: dict | None = None

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.received_scope = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _Recorder:
    """Captures ASGI `send` messages so a test can read the status/body the
    inner app (or `_deny`) actually emitted."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(m["body"] for m in self.messages if m["type"] == "http.response.body")


class _FakeRedis:
    """Minimal in-memory stand-in for `redis.asyncio.Redis` — only the calls
    the login rate limiter makes (`get`/`incr`/`ttl`/`expire`)."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        val = self._counts.get(key)
        return str(val) if val is not None else None

    async def incr(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    async def ttl(self, key: str) -> int:
        return self._ttls.get(key, -1)

    async def expire(self, key: str, seconds: int) -> None:
        self._ttls[key] = seconds


# ── cookie: sign / verify ───────────────────────────────────────────
def test_cookie_round_trips_for_a_valid_session() -> None:
    s = _gated_settings()
    value = _cookie_value(s, remember=True)
    assert _verify_cookie(value, s) is True


def test_tampered_cookie_fails_verification() -> None:
    s = _gated_settings()
    value = _cookie_value(s, remember=True)
    expiry, _, sig = value.partition(".")
    tampered = f"{expiry}.{'0' * len(sig)}"
    assert tampered != value
    assert _verify_cookie(tampered, s) is False


def test_garbage_cookie_fails_verification() -> None:
    s = _gated_settings()
    assert _verify_cookie("not-a-cookie", s) is False
    assert _verify_cookie("", s) is False
    assert _verify_cookie("nodot123456", s) is False


def test_expired_signed_cookie_fails_even_with_valid_signature() -> None:
    s = _gated_settings()
    key = _cookie_key(s.share_passphrase.get_secret_value())
    past = int(time.time()) - 10
    value = f"{past}.{_sign(past, key)}"  # correctly signed, but already expired
    assert _verify_cookie(value, s) is False


def test_verify_cookie_uses_constant_time_compare(monkeypatch) -> None:
    """Assert *behavior* (the constant-time primitive is invoked), not
    timing — a real timing side-channel test would be flaky in CI."""
    s = _gated_settings()
    value = _cookie_value(s, remember=True)
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", _spy)
    assert _verify_cookie(value, s) is True
    assert len(calls) == 1


def test_remember_false_signed_expiry_is_short_not_full_window() -> None:
    """FIX 1: the SIGNED expiry (not just Set-Cookie Max-Age) must be short
    when remember=False — a recovered raw cookie value must not stay valid
    for the full remembered window."""
    s = _gated_settings(share_session_days=30)
    before = int(time.time())
    value = _cookie_value(s, remember=False)
    expiry = int(value.split(".", 1)[0])
    ttl = expiry - before
    assert 0 < ttl <= _SHORT_SESSION_SECONDS + 2
    assert ttl < (s.share_session_days * 86400) / 2  # nowhere near the 30d window


def test_remember_true_signed_expiry_matches_share_session_days() -> None:
    s = _gated_settings(share_session_days=30)
    before = int(time.time())
    value = _cookie_value(s, remember=True)
    expiry = int(value.split(".", 1)[0])
    ttl = expiry - before
    assert abs(ttl - 30 * 86400) <= 2


# ── is_authenticated ─────────────────────────────────────────────────
def test_is_authenticated_true_when_gate_disabled() -> None:
    s = _settings()  # empty passphrase
    request = Request(_http_scope("/api/v1/session"))
    assert is_authenticated(request, s) is True


def test_is_authenticated_false_without_cookie() -> None:
    s = _gated_settings()
    request = Request(_http_scope("/api/v1/networth"))
    assert is_authenticated(request, s) is False


def test_is_authenticated_false_with_invalid_cookie() -> None:
    s = _gated_settings()
    request = Request(_http_scope("/api/v1/networth", cookie="garbage"))
    assert is_authenticated(request, s) is False


def test_is_authenticated_true_with_valid_cookie() -> None:
    s = _gated_settings()
    value = _cookie_value(s, remember=True)
    request = Request(_http_scope("/api/v1/networth", cookie=value))
    assert is_authenticated(request, s) is True


# ── path-normalisation primitives (FIX 2) ────────────────────────────
def test_collapse_slashes_collapses_duplicates_only() -> None:
    assert _collapse_slashes("//api/v1/networth") == "/api/v1/networth"
    assert _collapse_slashes("/api/v1//fava") == "/api/v1/fava"
    assert _collapse_slashes("/api/v1/networth") == "/api/v1/networth"
    assert _collapse_slashes("") == "/"


def test_has_dot_segment_detects_exact_segments_only() -> None:
    assert _has_dot_segment("/api/v1/../fava") is True
    assert _has_dot_segment("/api/v1/./networth") is True
    assert _has_dot_segment("/api/v1/networth") is False
    assert _has_dot_segment("/api/v1/network..s") is False  # not an exact segment


# ── middleware (ASGI) ─────────────────────────────────────────────────
async def _run(monkeypatch, settings: Settings, scope: dict) -> tuple[_InnerApp, _Recorder]:
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    inner = _InnerApp()
    mw = ShareGateMiddleware(inner)
    recorder = _Recorder()
    await mw(scope, _receive, recorder)
    return inner, recorder


@pytest.mark.parametrize(
    "path", ["/api/v1/health", "/api/v1/session", "/api/v1/auth/login", "/api/v1/auth/verify"]
)
async def test_exempt_paths_pass_without_a_cookie(monkeypatch, path) -> None:
    inner, recorder = await _run(monkeypatch, _gated_settings(), _http_scope(path))
    assert inner.called is True
    assert recorder.status == 200


async def test_gated_path_with_no_cookie_is_denied(monkeypatch) -> None:
    inner, recorder = await _run(monkeypatch, _gated_settings(), _http_scope("/api/v1/networth"))
    assert inner.called is False
    assert recorder.status == 401
    assert b"UNAUTHENTICATED" in recorder.body


async def test_gated_path_with_invalid_cookie_is_denied(monkeypatch) -> None:
    inner, recorder = await _run(
        monkeypatch, _gated_settings(), _http_scope("/api/v1/networth", cookie="garbage.sig")
    )
    assert inner.called is False
    assert recorder.status == 401


async def test_gated_path_with_valid_cookie_is_forwarded(monkeypatch) -> None:
    settings = _gated_settings()
    cookie_val = _cookie_value(settings, remember=True)
    inner, recorder = await _run(
        monkeypatch, settings, _http_scope("/api/v1/networth", cookie=cookie_val)
    )
    assert inner.called is True
    assert recorder.status == 200


async def test_gate_disabled_everything_passes_through(monkeypatch) -> None:
    inner, recorder = await _run(monkeypatch, _settings(), _http_scope("/api/v1/networth"))
    assert inner.called is True
    assert recorder.status == 200


async def test_websocket_scope_passed_through_untouched(monkeypatch) -> None:
    settings = _gated_settings()
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    inner = _InnerApp()
    mw = ShareGateMiddleware(inner)
    scope = {"type": "websocket", "path": "/api/v1/ws/prices"}
    await mw(scope, _receive, _Recorder())
    assert inner.called is True
    assert inner.received_scope is scope  # same object — no cookie check ran


async def test_dot_segment_path_denied_even_with_a_valid_cookie(monkeypatch) -> None:
    """`/api/v1/%2e%2e/fava` arrives here already percent-decoded by the ASGI
    server as a literal `..` segment (FIX 2) — denied outright, even carrying
    an otherwise-valid session cookie, proving the check runs unconditionally
    ahead of the exempt/auth branches."""
    settings = _gated_settings()
    cookie_val = _cookie_value(settings, remember=True)
    inner, recorder = await _run(
        monkeypatch, settings, _http_scope("/api/v1/../fava", cookie=cookie_val)
    )
    assert inner.called is False
    assert recorder.status == 401


async def test_double_slash_collapses_into_scope_and_stays_gated(monkeypatch) -> None:
    """Slash-collapsing can only make a path STRICTER to match (docstring's
    own example: `//api/v1/x` -> `/api/v1/x`) — a double-slash variant must
    still require the cookie, never slip through as unrecognised/exempt."""
    inner, recorder = await _run(monkeypatch, _gated_settings(), _http_scope("/api/v1//fava"))
    assert inner.called is False
    assert recorder.status == 401


# ── fail-closed (FIX 7) ───────────────────────────────────────────────
async def test_fail_closed_when_auth_check_raises(monkeypatch) -> None:
    settings = _gated_settings()
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)

    def _boom(request: Request, settings: Settings) -> bool:
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(auth_module, "is_authenticated", _boom)
    inner = _InnerApp()
    mw = ShareGateMiddleware(inner)
    recorder = _Recorder()
    await mw(_http_scope("/api/v1/networth"), _receive, recorder)
    assert inner.called is False
    assert recorder.status == 401


async def test_fail_closed_when_settings_lookup_raises(monkeypatch) -> None:
    def _boom() -> Settings:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(auth_module, "get_settings", _boom)
    inner = _InnerApp()
    mw = ShareGateMiddleware(inner)
    recorder = _Recorder()
    await mw(_http_scope("/api/v1/networth"), _receive, recorder)
    assert inner.called is False
    assert recorder.status == 401


# ── login rate limit ──────────────────────────────────────────────────
def test_client_ip_prefers_x_real_ip() -> None:
    scope = _http_scope(
        "/api/v1/auth/login",
        headers=[(b"x-real-ip", b"1.1.1.1"), (b"x-forwarded-for", b"2.2.2.2")],
    )
    assert _client_ip(Request(scope)) == "1.1.1.1"


def test_client_ip_falls_back_to_forwarded_for() -> None:
    scope = _http_scope("/api/v1/auth/login", headers=[(b"x-forwarded-for", b"3.3.3.3, 4.4.4.4")])
    assert _client_ip(Request(scope)) == "3.3.3.3"


def test_client_ip_falls_back_to_socket_client() -> None:
    scope = _http_scope("/api/v1/auth/login", client=("5.5.5.5", 1))
    assert _client_ip(Request(scope)) == "5.5.5.5"


async def test_login_fail_count_increments_per_ip() -> None:
    redis = _FakeRedis()
    assert await _login_fail_count(redis, "1.2.3.4") == 0
    await _register_login_failure(redis, "1.2.3.4")
    await _register_login_failure(redis, "1.2.3.4")
    assert await _login_fail_count(redis, "1.2.3.4") == 2
    assert await _login_fail_count(redis, "9.9.9.9") == 0  # different IP, untouched


async def test_n_failed_logins_then_429(monkeypatch) -> None:
    """Simulate N failed logins against a mocked Redis counter — the (N+1)th
    attempt must be rejected by the rate limiter before the passphrase is even
    compared."""
    settings = _gated_settings()
    redis = _FakeRedis()
    request = Request(_http_scope("/api/v1/auth/login", client=("9.9.9.9", 1)))
    body = LoginRequest(passphrase="definitely-wrong", remember=False)

    for _ in range(_LOGIN_MAX_ATTEMPTS):
        with pytest.raises(Unauthenticated):
            await login(body, request, settings, redis)

    with pytest.raises(RateLimitExceeded) as exc_info:
        await login(body, request, settings, redis)
    assert exc_info.value.status_code == 429


async def test_login_success_sets_a_verifiable_session_cookie() -> None:
    settings = _gated_settings()
    redis = _FakeRedis()
    request = Request(_http_scope("/api/v1/auth/login", client=("8.8.8.8", 1)))
    body = LoginRequest(passphrase=_PASSPHRASE, remember=True)
    response = await login(body, request, settings, redis)
    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie")
    assert set_cookie is not None and COOKIE_NAME in set_cookie


# ── config: passphrase floor (defence-in-depth for the brute-force window) ──
def test_short_passphrase_rejected_by_settings_validator() -> None:
    with pytest.raises(ValueError):
        _settings(share_passphrase="short1234")  # 9 chars, below the 12-char floor


def test_empty_passphrase_is_accepted_gate_disabled() -> None:
    s = _settings(share_passphrase="")
    assert s.share_passphrase.get_secret_value() == ""


def test_passphrase_exactly_at_floor_is_accepted() -> None:
    s = _settings(share_passphrase="x" * 12)
    assert s.share_passphrase.get_secret_value() == "x" * 12
