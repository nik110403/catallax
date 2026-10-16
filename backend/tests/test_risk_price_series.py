"""Risk price fetches preserve known holding classes through HistoryStore."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import Settings
from app.services import risk as risk_mod
from app.services.history import HistoryStore
from app.services.risk import RiskEngine


def _settings(**kw: object) -> Settings:
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **kw,
    )


@pytest.mark.parametrize(
    "crypto_hint,price_symbol,expected_yahoo_symbol",
    [
        (False, "SOL", "SOL"),
        (True, "SOL-USD", "SOL-USD"),
        (None, "SOL", "SOL-USD"),
    ],
)
async def test_risk_fetch_preserves_yahoo_hint_and_history_key(
    monkeypatch,
    crypto_hint: bool | None,
    price_symbol: str,
    expected_yahoo_symbol: str,
) -> None:
    """The caller-supplied Yahoo hint controls symbol mapping and cache keys."""
    captured: dict[str, object] = {}

    class _FakeAdapter:
        def __init__(
            self,
            redis: object,
            settings: object,
            *,
            crypto: bool | None = None,
            native_ticker: bool = False,
        ) -> None:
            captured["crypto"] = crypto
            captured["native_ticker"] = native_ticker
            self._settings = settings
            self._crypto_hint = crypto

        def _yf_symbol(self, symbol: str) -> str:
            return YFinanceAdapter(
                None, self._settings, crypto=self._crypto_hint
            )._yf_symbol(symbol)  # type: ignore[arg-type]

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    class _Env:
        data: list = []
        stale = False

    async def _fake_get_daily(self, adapter, symbol, start, end):  # noqa: ANN001
        captured["symbol"] = symbol
        return _Env()

    monkeypatch.setattr(risk_mod, "YFinanceAdapter", _FakeAdapter)
    monkeypatch.setattr(risk_mod, "session_scope", _fake_session_scope)
    monkeypatch.setattr(risk_mod.HistoryStore, "get_daily", _fake_get_daily)

    settings = _settings(crypto_yf_symbols="BTC,SOL")
    engine = RiskEngine(redis=object(), settings=settings)  # type: ignore[arg-type]
    await engine._fetch_one(
        price_symbol, "yfinance", crypto_hint, False, object(), object()
    )

    assert captured == {
        "crypto": crypto_hint,
        "native_ticker": False,
        "symbol": price_symbol,
    }
    assert (
        YFinanceAdapter(None, settings, crypto=captured["crypto"])._yf_symbol(
            price_symbol
        )
        == expected_yahoo_symbol
    )  # type: ignore[arg-type]


async def test_risk_fetch_specs_keep_yahoo_hints_and_nonholding_series_unknown(
    monkeypatch,
) -> None:
    engine = RiskEngine(redis=object(), settings=_settings())  # type: ignore[arg-type]
    captured: dict[str, object] = {}

    async def _fake_gather(specs, start, end):  # noqa: ANN001
        captured["specs"] = specs
        return {}

    monkeypatch.setattr(engine, "_gather", _fake_gather)
    await engine._fetch_series(
        [
            {
                "wf": "SOL_EQ",
                "asset_class": "EQUITY",
                "skip": None,
                "price_symbol": "SOL",
                "source": "yfinance",
                "currency": "USD",
                "crypto_hint": False,
            },
            {
                "wf": "SOL_CR",
                "asset_class": "CRYPTO",
                "skip": None,
                "price_symbol": "SOL-USD",
                "source": "yfinance",
                "currency": "USD",
                "crypto_hint": True,
                "native_yahoo_ticker": True,
            },
            {
                "wf": "SOL_OTHER",
                "asset_class": "OTHER",
                "skip": None,
                "price_symbol": "SOL",
                "source": "yfinance",
                "currency": "EUR",
                "crypto_hint": None,
            },
        ],
        lookback=1,
    )

    assert captured["specs"] == [
        ("A:SOL_EQ", "SOL", "yfinance", False, False),
        ("A:SOL_CR", "SOL-USD", "yfinance", True, True),
        ("A:SOL_OTHER", "SOL", "yfinance", None, False),
        ("FX:USD", "EURUSD=X", "yfinance", None, False),
        ("B:EQ", "^GSPC", "yfinance", None, False),
        ("B:CR", "BTC-USD", "yfinance", None, False),
    ]


async def test_risk_explicit_yahoo_override_stays_bare_for_crypto_holding(
    monkeypatch,
) -> None:
    """An explicit Yahoo override must not be remapped from SOL to SOL-USD."""
    settings = _settings(
        crypto_yf_symbols="BTC,SOL",
        risk_symbol_map="SOL:SOL:yfinance:USD",
    )
    engine = RiskEngine(redis=object(), settings=settings)  # type: ignore[arg-type]
    normal = RiskEngine(
        redis=object(), settings=_settings(crypto_yf_symbols="BTC,SOL")
    )._resolve("SOL", "CRYPTO")  # type: ignore[arg-type]
    resolved = engine._resolve_all(
        [
            type(
                "Holding", (), {"symbol": "SOL", "asset_class": "CRYPTO", "value": 1}
            )(),
        ]
    )
    captured: dict[str, object] = {}

    async def _fake_gather(specs, start, end):  # noqa: ANN001
        captured["specs"] = specs
        return {}

    monkeypatch.setattr(engine, "_gather", _fake_gather)
    await engine._fetch_series(resolved, lookback=1)

    assert captured["specs"][0] == ("A:SOL", "SOL", "yfinance", False, True)
    assert YFinanceAdapter(None, settings, crypto=False)._yf_symbol("SOL") == "SOL"
    assert normal["price_symbol"] == "SOL-USD"
    assert YFinanceAdapter(None, settings, crypto=True)._yf_symbol("SOL") == "SOL-USD"


async def test_risk_yahoo_override_bypasses_alias_for_adapter_and_history_key(
    monkeypatch,
) -> None:
    """An explicit Yahoo ticker is native, not input for the shared mapper."""
    settings = _settings(
        crypto_yf_symbols="BTC,SOL",
        yfinance_symbol_aliases="SOL=SOL-USD",
        risk_symbol_map="SOL:SOL:yfinance:USD",
    )
    engine = RiskEngine(redis=object(), settings=settings)  # type: ignore[arg-type]
    resolved = engine._resolve_all(
        [type("Holding", (), {"symbol": "SOL", "asset_class": "CRYPTO", "value": 1})()]
    )
    captured: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    class _Env:
        data: list = []
        stale = False

    async def _fake_get_daily(self, adapter, symbol, start, end):  # noqa: ANN001
        captured.append((symbol, adapter._yf_symbol("SOL")))
        return _Env()

    monkeypatch.setattr(risk_mod, "session_scope", _fake_session_scope)
    monkeypatch.setattr(risk_mod.HistoryStore, "get_daily", _fake_get_daily)
    await engine._fetch_series(resolved, lookback=1)

    assert captured[0] == ("SOL", "SOL")


async def test_risk_resolution_uses_canonical_yahoo_history_keys(monkeypatch) -> None:
    """Class hints survive resolution and unknown symbols share Yahoo's cache key."""
    settings = _settings(
        crypto_yf_symbols="BTC,SOL",
        risk_symbol_map="SOL_OVERRIDE:SOL:yfinance:USD",
    )
    engine = RiskEngine(redis=object(), settings=settings)  # type: ignore[arg-type]
    resolved = engine._resolve_all(
        [
            type(
                "Holding", (), {"symbol": "SOL", "asset_class": "EQUITY", "value": 1}
            )(),
            type(
                "Holding", (), {"symbol": "SOL", "asset_class": "CRYPTO", "value": 1}
            )(),
            type(
                "Holding", (), {"symbol": "SOL", "asset_class": "OTHER", "value": 1}
            )(),
            type(
                "Holding",
                (),
                {"symbol": "SOL_OVERRIDE", "asset_class": "CRYPTO", "value": 1},
            )(),
        ]
    )
    captured: list[tuple[str, bool | None]] = []

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    class _Env:
        data: list = []
        stale = False

    async def _fake_get_daily(self, adapter, symbol, start, end):  # noqa: ANN001
        captured.append((symbol, adapter._crypto_hint))
        return _Env()

    monkeypatch.setattr(risk_mod, "session_scope", _fake_session_scope)
    monkeypatch.setattr(risk_mod.HistoryStore, "get_daily", _fake_get_daily)
    await engine._fetch_series(resolved, lookback=1)

    assert [r["crypto_hint"] for r in resolved] == [False, True, None, False]
    assert captured[:4] == [
        ("SOL", False),
        ("SOL-USD", True),
        ("SOL", None),
        ("SOL", False),
    ]


async def test_risk_crypto_alias_is_resolved_once_for_history_store_and_yahoo(
    monkeypatch,
) -> None:
    """Risk passes its resolved crypto ticker through HistoryStore unchanged."""
    settings = _settings(
        crypto_yf_symbols="BTC,SOL",
        yfinance_symbol_aliases="SOL=SOL-USD,SOL-USD=SOL-CANON",
    )
    engine = RiskEngine(redis=_Redis(), settings=settings)  # type: ignore[arg-type]
    resolved = engine._resolve_all(
        [type("Holding", (), {"symbol": "SOL", "asset_class": "CRYPTO", "value": 1})()]
    )
    seen: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    async def _read_rows(self, symbol, source, start_d, end_d):  # noqa: ANN001
        seen.append(("db", symbol))
        return []

    async def _get_history(self, symbol, start, end, interval):  # noqa: ANN001
        seen.append(("yahoo", self._yf_symbol(symbol)))
        return []

    monkeypatch.setattr(risk_mod, "session_scope", _fake_session_scope)
    monkeypatch.setattr(HistoryStore, "_read_rows", _read_rows)
    monkeypatch.setattr(HistoryStore, "_needs_backfill", lambda *args: True)
    monkeypatch.setattr(YFinanceAdapter, "get_history", _get_history)

    await engine._fetch_series(resolved, lookback=1)

    assert resolved[0]["native_yahoo_ticker"] is True
    assert ("yahoo", "SOL-USD") in seen
    assert [symbol for kind, symbol in seen if kind == "db"].count("SOL-USD") == 2
    assert all(symbol != "SOL-CANON" for _, symbol in seen)


class _Redis:
    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str, *, ex: int) -> None:
        pass
