"""Phase 20 RSS adapters — httpx MockTransport, no network."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.adapters.events.base import Universe
from app.adapters.events.rss import RssSourceAdapter, build
from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "events" / "rss"
_REAL = httpx.AsyncClient


def _settings(**overrides) -> Settings:
    values = dict(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        news_feeds="https://feeds.test/markets.xml",
        events_cb_feeds="ECB=https://feeds.test/ecb.xml",
        events_ir_feeds="AAPL=https://feeds.test/ir.xml",
        events_user_agent="Catallax-Test/1.0",
        events_max_items_per_feed=100,
    )
    values.update(overrides)
    return Settings(**values)


def _xml(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _client(handler):
    def factory(*args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL(*args, **kwargs)

    return factory


def _poll(adapter):
    return asyncio.run(adapter.poll({}, None, None))


def test_build_news_cb_ir_slugs_and_hints():
    adapters = build(_settings(), None, Universe())
    by_source = {(item.source, item.feed): item for item in adapters}
    assert set(by_source) == {
        ("rss", "feeds.test/markets.xml"),
        ("central_bank", "ECB"),
        ("company_ir", "ir:AAPL"),
    }
    assert all(isinstance(item, RssSourceAdapter) for item in adapters)
    assert by_source[("company_ir", "ir:AAPL")]._hints == [
        ("symbol", "AAPL", "configured")
    ]
    assert by_source[("central_bank", "ECB")]._hints == [
        ("central_bank", "ECB", "configured")
    ]


def test_news_events_seen_after_request_start(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "Catallax-Test/1.0"
        if request.headers.get("If-None-Match"):
            raise AssertionError("unconditional first GET")
        return httpx.Response(
            200,
            content=_xml("news.xml"),
            headers={"ETag": '"n1"', "Last-Modified": "Wed, 01 Oct 2025 12:00:00 GMT"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(events_cb_feeds="", events_ir_feeds=""), None, Universe()
    )[0]
    start = datetime.now(timezone.utc)
    result = _poll(adapter)
    assert [item.source_key for item in result.events] == ["g-a", "g-b"]
    assert {item.event_type for item in result.events} == {"news"}
    assert result.events[0].entity_hints == []
    assert result.events[0].source == "rss"
    assert start <= result.events[0].first_seen_at
    assert result.events[0].first_seen_at == result.events[1].first_seen_at
    assert result.cursor["newest_published_at"].startswith("2025-10-01T12:00:00")
    assert result.etag == '"n1"'
    assert result.last_modified == "Wed, 01 Oct 2025 12:00:00 GMT"


def test_central_bank_types_and_configured_hint(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml("cb.xml"))

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = next(
        item
        for item in build(
            _settings(news_feeds="", events_ir_feeds=""), None, Universe()
        )
        if item.source == "central_bank"
    )
    result = _poll(adapter)
    types = {item.source_key: item.event_type for item in result.events}
    assert types == {
        "cb-rate": "rate_decision",
        "cb-speech": "speech",
        "cb-press": "press_release",
    }
    assert all(
        item.entity_hints == [("central_bank", "ECB", "configured")]
        for item in result.events
    )


def test_ir_release_configured_symbol(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml("ir.xml"))

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = next(
        item
        for item in build(
            _settings(news_feeds="", events_cb_feeds=""), None, Universe()
        )
        if item.source == "company_ir"
    )
    result = _poll(adapter)
    assert result.events[0].event_type == "ir_release"
    assert result.events[0].feed == "ir:AAPL"
    assert result.events[0].entity_hints == [("symbol", "AAPL", "configured")]


def test_304_not_modified_no_events(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.headers.get("If-None-Match") == '"n1"':
            return httpx.Response(304)
        return httpx.Response(200, content=_xml("news.xml"), headers={"ETag": '"n1"'})

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(events_cb_feeds="", events_ir_feeds=""), None, Universe()
    )[0]
    first = _poll(adapter)
    second = asyncio.run(adapter.poll(first.cursor, first.etag, first.last_modified))
    assert first.events
    assert second.not_modified is True
    assert second.events == []
    assert calls["n"] == 2


def test_missing_date_published_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml("nodate.xml"))

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(
            news_feeds="https://feeds.test/markets.xml",
            events_cb_feeds="",
            events_ir_feeds="",
        ),
        None,
        Universe(),
    )[0]
    result = _poll(adapter)
    assert result.events[0].published_at is None
    assert result.cursor == {}


def test_future_date_survives(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml("future.xml"))

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(
            news_feeds="https://feeds.test/markets.xml",
            events_cb_feeds="",
            events_ir_feeds="",
        ),
        None,
        Universe(),
    )[0]
    result = _poll(adapter)
    assert result.events[0].published_at.year == 2099


def test_304_last_modified(monkeypatch):
    stamp = "Wed, 01 Oct 2025 12:00:00 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-Modified-Since") == stamp:
            return httpx.Response(304)
        return httpx.Response(
            200, content=_xml("news.xml"), headers={"Last-Modified": stamp}
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(events_cb_feeds="", events_ir_feeds=""), None, Universe()
    )[0]
    first = _poll(adapter)
    second = asyncio.run(adapter.poll(first.cursor, first.etag, first.last_modified))
    assert first.last_modified == stamp
    assert second.not_modified is True
    assert second.events == []
    assert second.cursor == first.cursor


def test_retries_429_and_5xx(monkeypatch):
    statuses = [429, 503, 200]
    calls = {"n": 0}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.adapters.events.rss.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(calls["n"], len(statuses) - 1)]
        calls["n"] += 1
        if status == 200:
            return httpx.Response(200, content=_xml("news.xml"))
        return httpx.Response(status, headers={"Retry-After": "0"})

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(events_cb_feeds="", events_ir_feeds=""), None, Universe()
    )[0]
    result = _poll(adapter)
    assert calls["n"] == 3
    assert [item.source_key for item in result.events] == ["g-a", "g-b"]


def test_updated_date_without_pubdate(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml("updated.xml"))

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(events_cb_feeds="", events_ir_feeds=""), None, Universe()
    )[0]
    result = _poll(adapter)
    assert result.events[0].published_at.year == 2025
    assert result.events[0].published_at.month == 10
    assert result.events[0].published_at.day == 2
    assert result.cursor["newest_published_at"].startswith("2025-10-02T08:00:00")


def test_max_items_truncates(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml("news.xml"))

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(events_cb_feeds="", events_ir_feeds="", events_max_items_per_feed=1),
        None,
        Universe(),
    )[0]
    result = _poll(adapter)
    assert [item.source_key for item in result.events] == ["g-a"]
