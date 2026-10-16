from datetime import datetime, timezone
from decimal import Decimal

import pytest
from app.adapters.base import Quote
from app.adapters.broker import base as broker_base
from app.adapters.broker.base import PaperBroker
from app.adapters.broker.factory import build_broker, known_brokers
from app.config import get_settings
from app.errors import UpstreamError


def test_known_brokers_lists_paper_available():
    rows = known_brokers()
    by_name = {r["name"]: r for r in rows}
    assert by_name["paper"]["available"] is True
    assert by_name["nautilus"]["available"] is False
    assert by_name["alpaca-paper"]["available"] is False


def test_build_unavailable_broker_raises():
    with pytest.raises(ValueError):
        build_broker("nautilus", get_settings())


class _FakeAdapter:
    """Quotes GOOD, raises for BAD — mirrors test_fallback.py's fake-adapter style."""

    async def get_quote(self, symbol: str) -> Quote:
        if symbol == "BAD":
            raise UpstreamError("no data")
        return Quote(
            symbol=symbol, price=Decimal("100"), currency="USD",
            as_of=datetime.now(timezone.utc), source="fake",
        )


async def test_paper_broker_mark_tolerates_one_bad_symbol(monkeypatch):
    monkeypatch.setattr(broker_base, "get_redis", lambda: object())
    monkeypatch.setattr(broker_base, "get_price_adapter", lambda source, redis, settings: _FakeAdapter())
    broker = PaperBroker(get_settings())
    marks = await broker.mark(["GOOD", "BAD"])
    assert marks == {"GOOD": 100.0}
