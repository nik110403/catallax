"""Share-gate auth: signed session cookie, the ASGI middleware that enforces
it, and the public /auth/* routes that issue/revoke it.

Gate is entirely OFF (today's ungated single-user behaviour, unchanged) when
`SHARE_PASSPHRASE` is empty — the default. When it's set, every /api/v1/*
route except the boot/auth surface (`/api/v1/health`, `/api/v1/session`,
`/api/v1/auth/*`) requires a valid `catallax_session` cookie.

SECURITY — no TLS here: nginx (nginx/nginx.conf) listens on plain HTTP only,
and the session cookie's `secure` flag defaults OFF (`SHARE_COOKIE_SECURE`)
so login still works on that supported plaintext loopback/LAN deployment —
a `secure` cookie is silently dropped by the browser over plain HTTP. On
plain HTTP, the login passphrase (POST /auth/login) and the session cookie
both cross the wire in cleartext. Before exposing this beyond localhost —
LAN or internet — put a TLS-terminating layer in front of nginx (a VPN overlay,
a Cloudflare Tunnel, or a TLS-terminating reverse proxy) and set
`SHARE_COOKIE_SECURE=true`. Same note lives next to SHARE_PASSPHRASE in
`.env.example`.

The cookie is a stdlib-only signed value, no server-side session store:
`<expiry_epoch>.<hmac-sha256>` where the HMAC key is derived from the
passphrase itself (`sha256(passphrase)`) — rotating SHARE_PASSPHRASE silently
invalidates every outstanding session, no extra revocation bookkeeping needed.

`ShareGateMiddleware` is deliberately pure ASGI, NOT
`starlette.middleware.base.BaseHTTPMiddleware`: `add_middleware` wraps it
OUTSIDE FastAPI's own routing/exception middleware (see
`Starlette.build_middleware_stack`), so it sees every scope, including the
price-ticker WebSocket (`/api/v1/ws/prices`). A non-"http" scope is still
passed straight through this middleware untouched (writing a second ASGI
websocket-frame protocol in here isn't worth it) — but `routers/ws.py` now
calls `is_authenticated` itself, before `websocket.accept()`, reusing this
module's cookie verification directly. So the WS route is gated the same as
every other `/api/v1/*` route once a passphrase is set; only the enforcement
point moved from this middleware to the route handler (security review,
2026-08-15 — the socket had no connection cap either, which let an anonymous
caller burn the shared upstream rate-limit quota).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings, get_settings
from app.errors import CatallaxError, RateLimitExceeded
from app.redis import get_redis

logger = logging.getLogger("catallax.auth")

COOKIE_NAME = "catallax_session"

_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 10
# Global bucket, same window, a higher ceiling (security review, 2026-08-15;
# comment corrected 2026-08-16 — the prior wording implied this bucket fixes
# the per-IP lockout below; it doesn't). `_login_fail_count` (the per-IP
# bucket, `_LOGIN_MAX_ATTEMPTS = 10`) is still checked FIRST, on every
# request, unchanged. Behind the reverse-proxy/SSO edge every request shares
# ONE edge IP (`_client_ip` reads X-Real-IP/X-Forwarded-For), so 10 failed
# logins from ANY single edge client still locks out ALL edge clients for 15
# minutes via that per-IP bucket alone — this global bucket does not prevent
# that; it only adds a SECOND, higher, edge-wide ceiling (50) that also has
# to be cleared. Net effect: an edge deployment is still one careless client
# away from a 15-minute lockout for everyone behind it. A real per-user fix
# (e.g. keying off the authenticated SSO principal instead of IP) is
# out of scope here.
_LOGIN_GLOBAL_MAX_ATTEMPTS = 50
_LOGIN_GLOBAL_KEY = "ratelimit:auth:login:global"

# Signed-expiry TTL when "remember this device" is unchecked (FIX 1): kept
# short regardless of what the raw cookie value or browser Max-Age imply, so
# a recovered cookie can't be replayed for the full 30-day remembered window.
_SHORT_SESSION_SECONDS = 12 * 60 * 60

_API_PREFIX = "/api/v1"
_EXEMPT_EXACT = {"/api/v1/health", "/api/v1/session"}
_EXEMPT_PREFIX = "/api/v1/auth/"


# ── cookie: sign / verify ───────────────────────────────────────
def _cookie_key(passphrase: str) -> bytes:
    return hashlib.sha256(passphrase.encode()).digest()


def _sign(expiry: int, key: bytes) -> str:
    return hmac.new(key, str(expiry).encode(), hashlib.sha256).hexdigest()


def _cookie_value(settings: Settings, remember: bool) -> str:
    # FIX 1: the SIGNED expiry (not just the browser-side Max-Age in
    # `_set_session_cookie` below) must be short when remember=False, or a
    # recovered raw cookie value stays cryptographically valid for the full
    # remembered window no matter what the browser does with it.
    ttl = settings.share_session_days * 86400 if remember else _SHORT_SESSION_SECONDS
    expiry = int(time.time()) + ttl
    key = _cookie_key(settings.share_passphrase.get_secret_value())
    return f"{expiry}.{_sign(expiry, key)}"


def _verify_cookie(value: str, settings: Settings) -> bool:
    expiry_str, sep, sig = value.partition(".")
    if not sep or not sig:
        return False
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < int(time.time()):
        return False
    key = _cookie_key(settings.share_passphrase.get_secret_value())
    expected = _sign(expiry, key)
    # `hmac.compare_digest` raises TypeError on a non-ASCII `str` — encode
    # both sides to bytes first so a malformed/non-ASCII cookie value fails
    # closed (False) instead of raising (security review, 2026-08-15: this
    # path is called from `routers/ws.py` and `routers/session.py`, neither
    # of which wraps it in a try/except).
    return hmac.compare_digest(sig.encode("utf-8", errors="replace"), expected.encode())


def is_authenticated(conn: HTTPConnection, settings: Settings) -> bool:
    """Used by the middleware below, `routers/session.py`'s boot endpoint, and
    `routers/ws.py`'s pre-accept check. `HTTPConnection` is the common base of
    both `Request` and `WebSocket` (both just read `.cookies` off it), so this
    works unchanged for the websocket handshake — no duplicate cookie-parsing
    logic needed. Empty passphrase = gate disabled = everyone "authenticated"."""
    if not settings.share_passphrase.get_secret_value():
        return True
    value = conn.cookies.get(COOKIE_NAME)
    if not value:
        return False
    return _verify_cookie(value, settings)


def _set_session_cookie(response: Response, settings: Settings, remember: bool) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=_cookie_value(settings, remember),
        max_age=settings.share_session_days * 86400 if remember else None,
        httponly=True,
        samesite="lax",
        secure=settings.share_cookie_secure,
        path="/",
    )


class Unauthenticated(CatallaxError):
    """Missing/invalid session cookie, or a wrong login passphrase — same
    code+message either way so a client can't tell the two cases apart."""

    code = "UNAUTHENTICATED"
    status_code = 401
    public_message = "Passphrase required"


# ── middleware ───────────────────────────────────────────────────
def _collapse_slashes(raw: str) -> str:
    """Collapse duplicate slashes only — deliberately NOT resolving ./..
    segments (FIX 2). Starlette's router does neither dot-resolution nor
    slash-collapsing when matching `scope["path"]` against a route (exact
    literal segment match), so resolving dots here would let this exempt
    decision diverge from what the router actually does with the same raw
    path — unsound for an auth boundary. Slash-collapsing alone is safe: it
    can only ever make an in-scope path stricter to match (`//api/v1/x` ->
    `/api/v1/x`), never smuggle a dot segment past `_has_dot_segment` below."""
    return re.sub(r"/+", "/", raw or "/")


def _has_dot_segment(path: str) -> bool:
    """True if any segment of `path` is exactly "." or "..". `scope["path"]`
    is already percent-decoded by the ASGI server (e.g. a `%2e%2e` segment
    arrives here as literal ".."), so this also catches encoded-dot tricks.
    No real route needs a dot segment, so any in-scope request carrying one
    is rejected outright (FIX 2) rather than trusted to a normalize-then-
    compare exempt check that could diverge from the router's own literal,
    non-resolving match."""
    return any(segment in (".", "..") for segment in path.split("/"))


def _in_scope(path: str) -> bool:
    return path == _API_PREFIX or path.startswith(_API_PREFIX + "/")


def _is_exempt(path: str) -> bool:
    # Exact match only for health/session — a prefix match would let
    # /api/v1/healthz-not-really slip through ungated. `path` here is always
    # slash-collapsed and dot-segment-free: the middleware rejects any dot
    # segment via `_has_dot_segment` before this is ever consulted.
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIX)


async def _deny(scope: Scope, receive: Receive, send: Send) -> None:
    exc = Unauthenticated()
    response = JSONResponse(exc.envelope(), status_code=exc.status_code)
    await response(scope, receive, send)


class ShareGateMiddleware:
    """Pure-ASGI (see module docstring for why). Gates every in-scope
    /api/v1/* request behind the session cookie once SHARE_PASSPHRASE is set."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # FIX 7: explicit fail-closed. This middleware sits outside
        # `register_exception_handlers` (see module docstring), so an
        # unexpected exception here would otherwise propagate uncaught and
        # rely on implicit ASGI-server behaviour — make the contract
        # explicit. Every branch below either awaits `self.app(...)` and
        # returns, or awaits `_deny(...)` and returns; if any of them raises,
        # control lands in `except` and only `_deny(...)` runs from there —
        # no path reaches `self.app(...)` after an exception.
        try:
            settings = get_settings()
            if not settings.share_passphrase.get_secret_value():
                await self.app(scope, receive, send)
                return

            path = _collapse_slashes(scope.get("path", "/"))
            if not _in_scope(path):
                await self.app(scope, receive, send)
                return

            # FIX 2: reject dot segments outright instead of resolving them
            # for the exempt check — see `_has_dot_segment`'s docstring.
            if _has_dot_segment(path):
                await _deny(scope, receive, send)
                return

            if _is_exempt(path):
                await self.app(scope, receive, send)
                return

            request = Request(scope, receive=receive)
            if is_authenticated(request, settings):
                await self.app(scope, receive, send)
                return

            await _deny(scope, receive, send)
        except Exception:
            logger.exception("ShareGateMiddleware: unexpected error — failing closed")
            await _deny(scope, receive, send)


# ── /api/v1/auth/* routes (always public — see the exempt list above) ────
router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    # FIX 8: nginx is the sole ingress from OUTSIDE the compose network
    # (backend's 8000 is never published to the host — see
    # docker-compose.yml), so these headers can't be forged by an external
    # attacker. But any SIBLING container on the same compose network
    # (ai-runner, backtester, trading-engine, qlib-lab, fava) can reach
    # fastapi-backend:8000 directly and set X-Real-IP/X-Forwarded-For to
    # whatever it likes — e.g. to grief-lock the owner's real IP by forging
    # 10 failed logins against it. The real trust boundary is "no untrusted
    # code runs in this compose network", not "nginx is the only path in".
    # Never trust these headers on a directly internet-facing app regardless.
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_key(ip: str) -> str:
    return f"ratelimit:auth:login:{ip}"


async def _key_fail_count(redis: Redis, key: str) -> int:
    raw = await redis.get(key)
    return int(raw) if raw is not None else 0


async def _register_key_failure(redis: Redis, key: str) -> None:
    # Fixed 15-min window, same INCR + conditional-EXPIRE idiom as the
    # adapter rate limiter (adapters/base.py._enforce_rate_limit).
    count = await redis.incr(key)
    if count == 1 or await redis.ttl(key) < 0:
        await redis.expire(key, _LOGIN_WINDOW_SECONDS)


async def _login_fail_count(redis: Redis, ip: str) -> int:
    return await _key_fail_count(redis, _login_key(ip))


async def _register_login_failure(redis: Redis, ip: str) -> None:
    await _register_key_failure(redis, _login_key(ip))


class LoginRequest(BaseModel):
    passphrase: str
    remember: bool = False


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> Response:
    ip = _client_ip(request)
    if await _login_fail_count(redis, ip) >= _LOGIN_MAX_ATTEMPTS:
        logger.warning("share-gate login rate-limited: ip=%s", ip)
        raise RateLimitExceeded("too many failed login attempts")
    if await _key_fail_count(redis, _LOGIN_GLOBAL_KEY) >= _LOGIN_GLOBAL_MAX_ATTEMPTS:
        logger.warning("share-gate login rate-limited: global bucket exhausted")
        raise RateLimitExceeded("too many failed login attempts")

    # Compare only, constant-time — body.passphrase is never logged or
    # echoed, not even on failure (FIX 6 logs only the ip + outcome below).
    passphrase = settings.share_passphrase.get_secret_value()
    ok = bool(passphrase) and hmac.compare_digest(
        body.passphrase.encode(), passphrase.encode()
    )
    if not ok:
        await _register_login_failure(redis, ip)
        await _register_key_failure(redis, _LOGIN_GLOBAL_KEY)
        logger.warning("share-gate login failed: ip=%s", ip)
        raise Unauthenticated()

    response = Response(status_code=204)
    _set_session_cookie(response, settings, body.remember)
    return response


@router.post("/logout")
async def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/verify")
async def verify(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Backs nginx's `auth_request /_auth` (see nginx.conf). nginx discards
    the body and only looks at the status code, so these stay bare."""
    if not is_authenticated(request, settings):
        return Response(status_code=401)
    original_uri = request.headers.get("x-original-uri", "")
    if settings.demo_mode and original_uri.startswith("/fava"):
        # Demo mode: block even an authenticated visitor from the raw ledger UI.
        return Response(status_code=403)
    return Response(status_code=204)
