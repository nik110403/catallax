"""F2: the fallback chain skips failed sources and backs off on 429.

Verified with fake adapters so the contract is checked independently of any
upstream's live availability (Stooq is currently behind a bot-challenge from
datacenter IPs, so live cross-source failover can't be exercised from CI).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.adapters.base import Quote
from app.adapters.fallback import FallbackAdapter
from app.config import Settings
from app.errors import RateLimitExceeded, SymbolNotFound, UpstreamError


def _settings() -> Settings:
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        price_backoff_base_seconds=0.001,  # keep retries instant
        price_backoff_max_retries=1,
    )


def _quote(source: str) -> Quote:
    return Quote(
        symbol="AAPL",
        price=Decimal("100"),
        currency="USD",
        as_of=datetime.now(timezone.utc),
        source=source,
    )


class _Fake:
    """Minimal adapter stand-in: raises `exc` for its first `fail_times` calls
    (or every call if `fail_times` is None), then returns `quote`."""

    def __init__(self, name, *, quote=None, exc=None, fail_times=0):
        self.name = name
        self.asset_type = "equity"
        self._quote = quote
        self._exc = exc
        self._fail_times = fail_times
        self.calls = 0

    async def get_quote(self, symbol):
        self.calls += 1
        if self._exc is not None and (
            self._fail_times is None or self.calls <= self._fail_times
        ):
            raise self._exc
        return self._quote


def _wrap(chain) -> FallbackAdapter:
    return FallbackAdapter(chain, redis=object(), settings=_settings())


async def test_primary_fails_secondary_serves() -> None:
    primary = _Fake("yfinance", exc=UpstreamError("down"), fail_times=None)
    secondary = _Fake("stooq", quote=_quote("stooq"))
    fb = _wrap([primary, secondary])
    q = await fb.get_quote("AAPL")
    assert q.source == "stooq"
    assert secondary.calls == 1


async def test_429_backoff_then_retry_same_source() -> None:
    # Rate-limited once, then succeeds — retried in-place, no fall-through needed.
    flaky = _Fake(
        "yfinance", quote=_quote("yfinance"), exc=RateLimitExceeded("429"), fail_times=1
    )
    fb = _wrap([flaky])
    q = await fb.get_quote("AAPL")
    assert q.source == "yfinance"
    assert flaky.calls == 2  # one 429 + one success


async def test_429_exhausted_falls_through_to_next() -> None:
    primary = _Fake("yfinance", exc=RateLimitExceeded("429"), fail_times=None)
    secondary = _Fake("stooq", quote=_quote("stooq"))
    fb = _wrap([primary, secondary])
    q = await fb.get_quote("AAPL")
    assert q.source == "stooq"
    assert primary.calls == 2  # initial + 1 retry (max_retries=1), then gave up


async def test_all_sources_fail_raises_last() -> None:
    primary = _Fake("yfinance", exc=UpstreamError("down"), fail_times=None)
    secondary = _Fake("stooq", exc=SymbolNotFound("nope"), fail_times=None)
    fb = _wrap([primary, secondary])
    with pytest.raises(SymbolNotFound):
        await fb.get_quote("AAPL")
