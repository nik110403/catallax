"""Unit tests for the watchlist router's pure validate+normalize step
(`app/routers/watchlist.py::normalize_watchlist`).

Pure unit tests, no DB — mirrors this suite's convention (test_share_gate.py,
test_algo_engine.py's OrderSpec tests) of exercising validation logic in
isolation rather than standing up a real Postgres, which this container
doesn't have access to in CI (`docker compose run --no-deps`). The GET/PUT
routes' actual persistence (delete-all + insert-all via SQLAlchemy) is
exercised live against the running backend as part of the mandatory verify
step, not here.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import DemoModeReadOnly, InvalidRequest
from app.routers.watchlist import (
    _MAX_ITEMS,
    _MAX_SYMBOL_LEN,
    WatchlistItemIn,
    get_watchlist,
    normalize_watchlist,
    put_watchlist,
)


def _items(*pairs: tuple[str, str | None]) -> list[WatchlistItemIn]:
    return [WatchlistItemIn(symbol=sym, source=src) for sym, src in pairs]


def test_round_trip_preserves_symbols_and_sources():
    items = _items(("aapl", "yfinance"), ("btc", "coingecko"), ("^gspc", None))
    out = normalize_watchlist(items)
    assert [r["symbol"] for r in out] == ["AAPL", "BTC", "^GSPC"]
    assert [r["source"] for r in out] == ["yfinance", "coingecko", None]


def test_ordering_preserved_and_sequential():
    items = _items(("MSFT", None), ("AAPL", None), ("TSLA", None))
    out = normalize_watchlist(items)
    assert [r["symbol"] for r in out] == ["MSFT", "AAPL", "TSLA"]
    assert [r["sort_order"] for r in out] == [0, 1, 2]


def test_over_long_list_rejected():
    items = _items(*[(f"SYM{i}", None) for i in range(_MAX_ITEMS + 1)])
    with pytest.raises(InvalidRequest):
        normalize_watchlist(items)


def test_max_length_list_accepted():
    items = _items(*[(f"S{i}", None) for i in range(_MAX_ITEMS)])
    out = normalize_watchlist(items)
    assert len(out) == _MAX_ITEMS


def test_bad_symbol_rejected():
    with pytest.raises(InvalidRequest):
        normalize_watchlist(_items(("bad symbol", None)))
    with pytest.raises(InvalidRequest):
        normalize_watchlist(_items(("bad;symbol", None)))
    with pytest.raises(InvalidRequest):
        normalize_watchlist(_items(("", None)))


def test_symbol_too_long_rejected():
    with pytest.raises(InvalidRequest):
        normalize_watchlist(_items(("A" * (_MAX_SYMBOL_LEN + 1), None)))


def test_valid_symbol_forms_accepted():
    # yfinance-style forms: ^GSPC, EURUSD=X, GC=F, BRK-B.
    out = normalize_watchlist(_items(("^GSPC", None), ("EURUSD=X", None), ("GC=F", None), ("BRK-B", None)))
    assert [r["symbol"] for r in out] == ["^GSPC", "EURUSD=X", "GC=F", "BRK-B"]


def test_unknown_source_rejected():
    with pytest.raises(InvalidRequest):
        normalize_watchlist(_items(("AAPL", "robinhood")))


def test_known_sources_accepted():
    out = normalize_watchlist(_items(("AAPL", "yfinance"), ("BTC", "coingecko"), ("SPY", "stooq")))
    assert [r["source"] for r in out] == ["yfinance", "coingecko", "stooq"]


def test_duplicate_symbol_rejected():
    with pytest.raises(InvalidRequest):
        normalize_watchlist(_items(("AAPL", None), ("aapl", None)))


def test_pure_punctuation_symbol_rejected():
    # Security review (2026-08-15): pure-punctuation symbols pass the old
    # charset-only regex; must be rejected for lacking any alnum.
    for bad in ("^^", "==", "--", "."):
        with pytest.raises(InvalidRequest):
            normalize_watchlist(_items((bad, None)))


def test_dot_dot_symbol_rejected():
    # ".." reaches yfinance_adapter's upstream URL path unchanged.
    for bad in ("..", "AAPL..", "..AAPL"):
        with pytest.raises(InvalidRequest):
            normalize_watchlist(_items((bad, None)))


def test_error_does_not_echo_raw_symbol():
    exc_info = None
    try:
        normalize_watchlist(_items(("bad symbol", None)))
    except InvalidRequest as exc:
        exc_info = exc
    assert exc_info is not None
    assert "bad symbol" not in exc_info.envelope()["error"]["message"]
    assert "index 0" in exc_info.envelope()["error"]["message"]


# ── DEMO_MODE (security review, 2026-08-15) ─────────────────────────
def _settings(**kw: object) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


class _DBTouched(Exception):
    """Raised the instant the fake DB session is used — proves execution
    reached a real DB read/write, i.e. the demo_mode guard upstream was NOT
    taken."""


class _BoomDB:
    async def execute(self, *args: object, **kwargs: object) -> None:
        raise _DBTouched()


async def test_get_watchlist_empty_in_demo_mode():
    out = await get_watchlist(db=_BoomDB(), settings=_settings(demo_mode=True))
    assert out == []


async def test_get_watchlist_reaches_db_when_demo_off():
    with pytest.raises(_DBTouched):
        await get_watchlist(db=_BoomDB(), settings=_settings(demo_mode=False))


async def test_put_watchlist_rejected_in_demo_mode():
    with pytest.raises(DemoModeReadOnly):
        await put_watchlist(
            _items(("AAPL", None)), db=_BoomDB(), settings=_settings(demo_mode=True)
        )


async def test_put_watchlist_reaches_db_when_demo_off():
    with pytest.raises(_DBTouched):
        await put_watchlist(
            _items(("AAPL", None)), db=_BoomDB(), settings=_settings(demo_mode=False)
        )
