"""YFinanceAdapter._yf_symbol: explicit aliases, then the bare-crypto rule."""
from __future__ import annotations

from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import Settings


def _adapter(**kw: object) -> YFinanceAdapter:
    settings = Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )
    return YFinanceAdapter(redis=None, settings=settings)  # type: ignore[arg-type]


def test_alias_map_parses_and_ignores_malformed_pairs() -> None:
    s = Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r",
        yfinance_symbol_aliases=" sap = SAP.DE ,junk,=X.DE,EXMPL-1=EXMPL1-USD,",
    )
    assert s.yfinance_symbol_alias_map == {"SAP": "SAP.DE", "EXMPL-1": "EXMPL1-USD"}


def test_alias_wins_over_suffix_passthrough_and_crypto_rule() -> None:
    a = _adapter(
        yfinance_symbol_aliases="SAP=SAP.DE,FICTIVE-ECOSYSTEM-TOKEN=EXMPL3-USD,BTC=BTC-EUR",
        crypto_yf_symbols="BTC,EXMPL6",
    )
    assert a._yf_symbol("sap") == "SAP.DE"
    assert a._yf_symbol("FICTIVE-ECOSYSTEM-TOKEN") == "EXMPL3-USD"
    assert a._yf_symbol("BTC") == "BTC-EUR"


def test_unaliased_symbols_keep_existing_rules() -> None:
    a = _adapter(crypto_yf_symbols="BTC,EXMPL6")
    assert a._yf_symbol("EXMPL6") == "EXMPL6-USD"
    assert a._yf_symbol("VWCE.DE") == "VWCE.DE"
    assert a._yf_symbol("AAPL") == "AAPL"


# ── shared helper (every Yahoo path maps through ONE function) ────
from app.adapters.yahoo_symbols import crypto_hint, is_crypto_symbol, to_yahoo_symbol  # noqa: E402


def _settings(**kw: object) -> Settings:
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


def test_shared_helper_matches_the_adapter_rules() -> None:
    s = _settings(yfinance_symbol_aliases="SAP=SAP.DE,EXMPLCOIN=EXMPL2-USD", crypto_yf_symbols="BTC,EXMPL6")
    assert to_yahoo_symbol("sap", s) == "SAP.DE"
    assert to_yahoo_symbol("EXMPLCOIN", s) == "EXMPL2-USD"
    assert to_yahoo_symbol("EXMPL6", s) == "EXMPL6-USD"
    assert to_yahoo_symbol("VWCE.DE", s) == "VWCE.DE"
    assert to_yahoo_symbol("AAPL", s) == "AAPL"


def test_crypto_class_hint_prefers_the_alias_over_the_naive_usd_pair() -> None:
    """risk/report-worker resolve WF CRYPTO holdings — the alias must win
    (EXMPLCOIN -> EXMPL2-USD, never the non-existent EXMPLCOIN-USD)."""
    s = _settings(yfinance_symbol_aliases="EXMPLCOIN=EXMPL2-USD", crypto_yf_symbols="BTC")
    assert to_yahoo_symbol("EXMPLCOIN", s, crypto=True) == "EXMPL2-USD"
    assert to_yahoo_symbol("EXMPL7", s, crypto=True) == "EXMPL7-USD"   # not in the set, class says crypto
    assert to_yahoo_symbol("BTC-USD", s, crypto=True) == "BTC-USD"  # never BTC-USD-USD


def test_is_crypto_symbol_covers_set_members_and_usd_pair_aliases() -> None:
    s = _settings(yfinance_symbol_aliases="SAP=SAP.DE,EXMPLCOIN=EXMPL2-USD", crypto_yf_symbols="BTC,EXMPL5")
    assert is_crypto_symbol("exmpl5", s)
    assert is_crypto_symbol("EXMPLCOIN", s)
    assert is_crypto_symbol("ETH-USD", s)
    assert not is_crypto_symbol("SAP", s)
    assert not is_crypto_symbol("AAPL", s)
    assert not is_crypto_symbol("BRK-B", s)
    assert not is_crypto_symbol("EURUSD=X", s)


# ── review-225d5f4 re-review F1: exporter's "OTHER" default == blank ────────
def test_crypto_hint_treats_other_the_same_as_blank() -> None:
    """The Wealthfolio exporter defaults a missing instrument_type to "OTHER" —
    a coin exported that way must still get the heuristic (None), not be
    mistaken for a KNOWN non-crypto class (which would skip the -USD pair and
    404 against Yahoo's bare ticker)."""
    assert crypto_hint("OTHER") is None
    assert crypto_hint("other") is None
    assert crypto_hint("  Other  ") is None
    assert crypto_hint("CRYPTO") is True
    assert crypto_hint("EQUITY") is False
    assert crypto_hint(None) is None
    assert crypto_hint("") is None


def test_risk_resolve_crypto_class_honours_aliases() -> None:
    from app.services.risk import RiskEngine

    eng = RiskEngine.__new__(RiskEngine)
    eng._settings = _settings(yfinance_symbol_aliases="EXMPLCOIN=EXMPL2-USD", risk_skip_symbols="USDC")
    assert eng._resolve("EXMPLCOIN", "CRYPTO")["price_symbol"] == "EXMPL2-USD"
    assert eng._resolve("EXMPL7", "CRYPTO")["price_symbol"] == "EXMPL7-USD"
    assert eng._resolve("AAPL", "EQUITY")["price_symbol"] == "AAPL"


# ── review-225d5f4 re-review F2: the class hint must survive to the ONE
# resolution point the adapter actually calls, not just the caller's math ──
def test_adapter_binds_the_crypto_hint_once_so_a_colliding_equity_stays_bare() -> None:
    """A per-call default-`None` heuristic re-applied on top of an
    already-resolved bare ticker is exactly how a `SOL` EQUITY got remapped
    onto the `SOL-USD` coin: `to_yahoo_symbol(sym, crypto=False)` alone
    returns the correct bare `SOL`, but a second, hint-less call on that
    result mangles it again. Binding the hint at construction closes that gap."""
    a = _adapter(crypto_yf_symbols="BTC,SOL,EXMPL5")
    assert YFinanceAdapter(None, a._settings, crypto=False)._yf_symbol("SOL") == "SOL"  # type: ignore[arg-type]
    assert YFinanceAdapter(None, a._settings, crypto=True)._yf_symbol("SOL") == "SOL-USD"  # type: ignore[arg-type]
    assert YFinanceAdapter(None, a._settings)._yf_symbol("SOL") == "SOL-USD"  # unknown -> heuristic


def test_known_asset_class_overrides_the_ticker_heuristic() -> None:
    """review-225d5f4 finding 4: class beats spelling. `crypto=False` means
    the caller KNOWS it is not a coin (no CRYPTO_YF_SYMBOLS lookup); None
    means unknown (heuristic); aliases always win."""
    s = _settings(crypto_yf_symbols="BTC,SOL", yfinance_symbol_aliases="SAP=SAP.DE")
    assert to_yahoo_symbol("SOL", s) == "SOL-USD"                 # unknown -> heuristic
    assert to_yahoo_symbol("SOL", s, crypto=False) == "SOL"       # known equity
    assert to_yahoo_symbol("SAP", s, crypto=False) == "SAP.DE"  # alias still wins
    assert not is_crypto_symbol("SOL", s, asset_class="EQUITY")
    assert is_crypto_symbol("EXMPL7", s, asset_class="CRYPTO")
    assert is_crypto_symbol("SOL", s, asset_class=None)           # unknown -> heuristic
    assert is_crypto_symbol("SOL", s, asset_class="")
