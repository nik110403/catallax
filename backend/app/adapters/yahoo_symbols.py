"""The ONE Wealthfolio-symbol -> Yahoo-ticker mapping (every yfinance path).

Rules, in order:
  1. `YFINANCE_SYMBOL_ALIASES` explicit entry wins (SAP -> SAP.DE,
     EXMPLCOIN -> EXMPL2-USD).
  2. Crypto (the caller knows the asset class is crypto, or — class
     unknown — a bare ticker in `CRYPTO_YF_SYMBOLS`) -> Yahoo's `<SYM>-USD`
     pair. A KNOWN non-crypto class skips the spelling heuristic.
  3. Anything already carrying a market suffix (-, =, .) passes through
     (VWCE.DE, EURUSD=X, BRK-B, BTC-USD); else identity.

Every `yf.Ticker(...)` call and every class-aware resolver (risk, the report
worker) must go through `to_yahoo_symbol` — a raw WF symbol sent to Yahoo is
how a bare coin ticker without its `-USD` pair ends up as an HTTP 404.
"""
from __future__ import annotations

from app.config import Settings

_USD_PAIR = "-USD"


def to_yahoo_symbol(symbol: str, settings: Settings, *, crypto: bool | None = None) -> str:
    """Map a WF/user symbol to the ticker Yahoo resolves. `crypto` is the
    caller's KNOWN asset class (from Wealthfolio): True forces the USD pair
    for bare tickers outside `CRYPTO_YF_SYMBOLS`; False means known
    non-crypto, so the `CRYPTO_YF_SYMBOLS` spelling heuristic is skipped (an
    equity ticked `SOL` stays `SOL`); None (class unknown) uses the heuristic.
    An explicit alias always wins."""
    s = symbol.upper().strip()
    alias = settings.yfinance_symbol_alias_map.get(s)
    if alias:
        return alias
    if crypto:
        return s if s.endswith(_USD_PAIR) else f"{s}{_USD_PAIR}"
    if crypto is False or any(c in s for c in "-=."):
        return symbol
    return f"{s}{_USD_PAIR}" if s in settings.crypto_yf_symbol_set else symbol


def crypto_hint(asset_class: str | None) -> bool | None:
    """WF asset class -> the `crypto` hint: None when the class is unknown
    (missing/blank, or `OTHER` — the Wealthfolio exporter's default for a
    missing instrument_type is not a real classification), else whether it
    is `CRYPTO` (review 225d5f4 re-review F1)."""
    if not asset_class or not asset_class.strip():
        return None
    normalized = asset_class.strip().upper()
    if normalized == "OTHER":
        return None
    return normalized == "CRYPTO"


def is_crypto_symbol(symbol: str, settings: Settings, *, asset_class: str | None = None) -> bool:
    """Whether the holding is a coin — coins carry no equity statements, so
    fundamentals skip them without a call. A KNOWN asset class decides
    (review 225d5f4: an equity spelled like a listed coin is still an
    equity); only when the class is unknown does it fall back to the ticker
    heuristic — a listed bare coin, an alias onto a `...-USD` ticker, or an
    explicit `XXX-USD`."""
    hint = crypto_hint(asset_class)
    if hint is not None:
        return hint
    return to_yahoo_symbol(symbol, settings).upper().endswith(_USD_PAIR)
