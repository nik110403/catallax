"""WebSocket price ticker.

Streams live quotes for a set of symbols on an interval. Symbols come from the
`?symbols=BTC,AAPL` query param (per-symbol source via `SYM:source`, e.g.
`?symbols=BTC:coingecko,AAPL`) and the param is **required** — the old
empty-means-persisted-watchlist fallback would echo the owner's watchlist to
any caller. `?source=` sets the default adapter for symbols without an
explicit source. Each quote goes through the same adapter cache/rate-limit
path as the REST endpoints.

Security review (2026-08-15): `ShareGateMiddleware` passes non-"http" ASGI
scopes straight through (see `auth.py`'s module docstring), so this socket
authenticates itself, before `accept()`, reusing `auth.is_authenticated`
directly on the `WebSocket` connection. A connected socket is also bounded —
`WS_MAX_CONNECTIONS` concurrent sockets and `WS_MAX_LIFETIME_SECONDS` per
socket — so one caller can't hold the shared upstream-adapter rate-limit
quota hostage indefinitely.

Rejecting before `accept()` is deliberate (correct security posture — a
socket that never accepted the handshake can't stream anything), but it has
a client-visible cost: uvicorn turns a pre-accept `websocket.close(code=...)`
into a bare HTTP 403 at the handshake — the numeric close code (4401 auth,
4429 cap) and its reason never reach the client. Auth failure and cap
exhaustion are therefore indistinguishable to a client; both surface only as
"handshake rejected, HTTP 403".
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.adapters.base import PriceSourceAdapter
from app.adapters.factory import build_adapter
from app.auth import is_authenticated
from app.config import Settings, get_settings
from app.errors import CatallaxError
from app.redis import get_redis

logger = logging.getLogger("catallax.ws")
router = APIRouter()

_MAX_SYMBOLS = 50
_MAX_SYMBOL_LEN = 16
# Real symbols in use: bare tickers (AAPL, BRK-B), index/vol prefixes (^GSPC,
# ^VIX), FX pairs (EURUSD=X), futures (GC=F). Anything outside this charset
# reaches `yf.Ticker(...)` (adapters/yfinance_adapter.py), which interpolates
# the symbol into an upstream URL path — so reject rather than sanitize.
# Requires at least one alnum (rejects pure-punctuation like "^^" or "==")
# and forbids ".." anywhere (security review, 2026-08-15: ".." reaches the
# upstream URL path unchanged, e.g. /v8/finance/chart/..).
_SYMBOL_RE = re.compile(r"^(?!.*\.\.)(?=.*[A-Z0-9])[A-Z0-9.^=-]{1,16}$")

# Simple in-process cap — single uvicorn worker (see Dockerfile), no need for
# a Redis-shared counter.
_active_connections = 0


def _parse_query_symbols(raw: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            sym, _, src = token.partition(":")
            sym = sym.strip().upper()[:_MAX_SYMBOL_LEN]
            src = src.strip().lower() or None
        else:
            sym = token.upper()[:_MAX_SYMBOL_LEN]
            src = None
        if not _SYMBOL_RE.match(sym):
            continue
        out.append((sym, src))
        if len(out) >= _MAX_SYMBOLS:
            break
    return out


@router.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket) -> None:
    global _active_connections
    settings = get_settings()

    # Gate BEFORE accept — same cookie check as every other /api/v1/* route
    # once SHARE_PASSPHRASE is set. Gate disabled (no passphrase) = everyone
    # authenticated, same as today.
    if not is_authenticated(websocket, settings):
        await websocket.close(code=4401)
        return

    if _active_connections >= settings.ws_max_connections:
        await websocket.close(code=4429)
        return

    # Increment BEFORE accept(), not after (security review, 2026-08-15): the
    # old order let N concurrent handshakes all pass the check above before
    # any of them incremented, overshooting the cap by the handshake
    # concurrency. No `await` sits between the check and this increment, so
    # (single uvicorn worker — see module docstring) nothing can interleave
    # here. `finally` balances every path below, including `accept()` itself
    # raising, so the counter never leaks.
    _active_connections += 1
    try:
        await websocket.accept()
        await _stream(websocket, settings)
    finally:
        _active_connections -= 1


async def _stream(websocket: WebSocket, settings: Settings) -> None:
    redis = get_redis()

    raw = websocket.query_params.get("symbols", "")
    default_source = (
        websocket.query_params.get("source") or settings.price_source_default
    )

    # `?symbols=` is REQUIRED. Clients name their symbols explicitly; the
    # frontend's `useTicker` already never connects without them.
    symbols = _parse_query_symbols(raw) if raw.strip() else []

    if not symbols:
        await websocket.send_json(
            {"error": {"code": "NO_SYMBOLS", "message": "No symbols to stream"}}
        )
        await websocket.close()
        return

    # Build each adapter at most once per connection, keyed by source name.
    adapters: dict[str, PriceSourceAdapter] = {}

    def adapter_for(source: str) -> PriceSourceAdapter:
        if source not in adapters:
            adapters[source] = build_adapter(source, redis, settings)
        return adapters[source]

    deadline = time.monotonic() + settings.ws_max_lifetime_seconds
    try:
        while True:
            if time.monotonic() >= deadline:
                await websocket.close(code=1001, reason="max connection lifetime reached")
                return
            tick: dict[str, object] = {}
            for symbol, src in symbols:
                try:
                    quote = await adapter_for(src or default_source).get_quote(symbol)
                    tick[symbol] = {
                        "price": str(quote.price),
                        "currency": quote.currency,
                        "as_of": quote.as_of.isoformat(),
                        "source": quote.source,
                    }
                except CatallaxError as exc:
                    # Bad source name or upstream failure for one symbol must not
                    # tear down the whole stream. Use public_message so upstream
                    # detail isn't leaked over the socket.
                    tick[symbol] = {
                        "error": {
                            "code": exc.code,
                            "message": exc.public_message or exc.message,
                        }
                    }
            await websocket.send_json({"prices": tick})
            await asyncio.sleep(settings.ticker_interval_seconds)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.warning("WS ticker closing on unexpected error: %s", exc)
        await websocket.close()
