"""ResearchEngine._price_series threads the holding's KNOWN asset_class into
the adapter's crypto hint (review 225d5f4 re-review F2): a colliding ticker
(e.g. equity `SOL`) must not resolve prices onto the `SOL-USD` coin.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.config import Settings
from app.services import research as research_mod
from app.services.research import ResearchEngine


def _settings(**kw: object) -> Settings:
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


class _EmptyEnv:
    data: list = []


@pytest.mark.parametrize(
    "asset_class,expected_hint",
    [
        ("EQUITY", False),   # known non-crypto -> never the coin mapping
        ("CRYPTO", True),    # known crypto -> the USD pair
        ("OTHER", None),     # WF exporter default for a missing type -> unknown
        (None, None),        # unknown (e.g. Research tab) -> the spelling heuristic
    ],
)
async def test_price_series_threads_asset_class_into_the_adapter_crypto_hint(
    monkeypatch, asset_class, expected_hint
) -> None:
    captured: dict[str, object] = {}

    class _FakeAdapter:
        def __init__(self, redis: object, settings: object, *, crypto: bool | None = None) -> None:
            captured["crypto"] = crypto

    monkeypatch.setattr(research_mod, "YFinanceAdapter", _FakeAdapter)

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    monkeypatch.setattr(research_mod, "session_scope", _fake_session_scope)

    async def _fake_get_daily(self, adapter, symbol, start, end):  # noqa: ANN001
        return _EmptyEnv()

    monkeypatch.setattr(research_mod.HistoryStore, "get_daily", _fake_get_daily)

    engine = ResearchEngine(redis=object(), settings=_settings(crypto_yf_symbols="BTC,SOL"))  # type: ignore[arg-type]
    result = await engine._price_series("SOL", asset_class=asset_class)

    assert result is None  # empty candle envelope -> None, unrelated to the fix
    assert captured["crypto"] is expected_hint
