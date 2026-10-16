"""HistoryStore keys Yahoo bars by the adapter-resolved ticker."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.adapters.envelope import Envelope
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import Settings
from app.services.history import HistoryStore


def _settings(**kw: object) -> Settings:
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **kw,
    )


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_keys: list[str] = []
        self.set_keys: list[str] = []

    async def get(self, key: str) -> str | None:
        self.get_keys.append(key)
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.set_keys.append(key)
        self.values[key] = value


@pytest.mark.parametrize(
    "asset_class,expected",
    [("EQUITY", "SOL"), ("OTHER", "SOL-USD"), ("CRYPTO", "SOL-USD")],
)
async def test_yahoo_history_cache_key_uses_resolved_ticker(
    asset_class: str, expected: str
) -> None:
    settings = _settings(crypto_yf_symbols="BTC,SOL")
    redis = _Redis()
    adapter = YFinanceAdapter(
        redis,
        settings,
        crypto={"EQUITY": False, "OTHER": None, "CRYPTO": True}[asset_class],
    )
    store = HistoryStore(object(), redis, settings)  # type: ignore[arg-type]
    start = end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    redis.values[f"history:yfinance:{expected}:2026-09-01:2026-09-01"] = Envelope.fresh(
        [], source="yfinance"
    ).model_dump_json()

    await store.get_daily(adapter, "SOL", start, end)

    assert redis.get_keys == [f"history:yfinance:{expected}:2026-09-01:2026-09-01"]


async def test_yahoo_history_db_backfill_and_envelope_use_resolved_ticker(
    monkeypatch,
) -> None:
    settings = _settings(
        crypto_yf_symbols="BTC,SOL",
        yfinance_symbol_aliases="SOL=SOL-USD,SOL-USD=SOL-CANON",
    )
    redis = _Redis()
    adapter = YFinanceAdapter(redis, settings, crypto=True)
    store = HistoryStore(object(), redis, settings)  # type: ignore[arg-type]
    seen: list[tuple[str, str]] = []
    start = end = datetime(2026, 9, 1, tzinfo=timezone.utc)

    async def _read(symbol, source, start_d, end_d):  # noqa: ANN001
        seen.append(("read", symbol))
        return [
            type(
                "Row",
                (),
                {
                    "bar_date": start_d,
                    "open": Decimal("1"),
                    "high": Decimal("1"),
                    "low": Decimal("1"),
                    "close": Decimal("1"),
                    "volume": None,
                },
            )()
        ]

    async def _backfill(adapter, request_symbol, symbol, source, start, end):  # noqa: ANN001
        seen.append(("backfill", f"{request_symbol}|{symbol}"))

    monkeypatch.setattr(store, "_read_rows", _read)
    monkeypatch.setattr(store, "_backfill", _backfill)
    monkeypatch.setattr(store, "_needs_backfill", lambda *args: True)

    env = await store.get_daily(adapter, "SOL", start, end)

    assert seen == [
        ("read", "SOL-USD"),
        ("backfill", "SOL|SOL-USD"),
        ("read", "SOL-USD"),
    ]
    assert env.source == "yfinance"
    assert redis.set_keys == ["history:yfinance:SOL-USD:2026-09-01:2026-09-01"]


async def test_yahoo_history_alias_is_not_resolved_twice() -> None:
    settings = _settings(
        crypto_yf_symbols="BTC,SOL",
        yfinance_symbol_aliases="SOL=SOL-USD,SOL-USD=SOL-CANON",
    )
    adapter = YFinanceAdapter(_Redis(), settings, crypto=None)

    assert adapter.history_symbol("SOL") == "SOL-USD"


async def test_native_yahoo_history_ticker_bypasses_alias() -> None:
    settings = _settings(
        crypto_yf_symbols="BTC,SOL", yfinance_symbol_aliases="SOL=SOL-USD"
    )
    redis = _Redis()
    adapter = YFinanceAdapter(redis, settings, crypto=False, native_ticker=True)
    store = HistoryStore(object(), redis, settings)  # type: ignore[arg-type]
    start = end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    redis.values["history:yfinance:SOL:2026-09-01:2026-09-01"] = Envelope.fresh(
        [], source="yfinance"
    ).model_dump_json()

    await store.get_daily(adapter, "SOL", start, end)

    assert redis.get_keys == ["history:yfinance:SOL:2026-09-01:2026-09-01"]
