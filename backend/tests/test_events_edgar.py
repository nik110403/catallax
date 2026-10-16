"""Phase 20 EDGAR adapter — httpx MockTransport, no network."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from app.adapters.events.base import Universe
from app.adapters.events.edgar import _TICKERS_KEY, _val_key, build
from app.config import Settings
from app.models import MarketEvent
from app.services.events_ingest import ingest
from tests.test_events_ingest import NOW, _with_db

FIXTURES = Path(__file__).parent / "fixtures" / "events" / "edgar"
_REAL = httpx.AsyncClient
_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_AAPL = "https://data.sec.gov/submissions/CIK0000320193.json"
_MSFT = "https://data.sec.gov/submissions/CIK0000789019.json"
_IBM = "https://data.sec.gov/submissions/CIK0000051143.json"
_EMPTY = json.dumps(
    {
        "cik": "0",
        "name": "X",
        "filings": {
            "recent": {
                "accessionNumber": [],
                "form": [],
                "acceptanceDateTime": [],
                "filingDate": [],
                "reportDate": [],
            }
        },
    }
).encode()


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expires: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.expires[key] = ex


def _settings(**overrides) -> Settings:
    values = dict(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        sec_edgar_user_agent="Catallax Test test@example.test",
        sec_edgar_rate_limit_per_sec=5,
        sec_edgar_forms="4,8-K,6-K,SC 13D,SC 13G",
        events_adapter_timeout_seconds=120,
        events_http_timeout_seconds=20,
        events_max_items_per_feed=100,
    )
    values.update(overrides)
    return Settings(**values)


def _universe(*symbols: str, cik_map: dict[str, str] | None = None) -> Universe:
    return Universe(symbols=frozenset(symbols), cik_map=cik_map or {})


def _json(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _client(handler):
    def factory(*args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL(*args, **kwargs)

    return factory


def _map(body: dict[str, bytes], hits: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if hits is not None:
            hits.append(url)
        if request.headers.get("user-agent") != "Catallax Test test@example.test":
            raise AssertionError("SEC UA missing")
        if request.headers.get("accept-encoding") != "gzip":
            raise AssertionError("gzip required")
        payload = body.get(url)
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, content=payload)

    return handler


def test_build_disabled_without_ua_or_universe_makes_no_http(monkeypatch):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    assert build(_settings(sec_edgar_user_agent=""), _Redis(), _universe("AAPL")) == []
    assert build(_settings(), _Redis(), None) == []
    assert hits == []


def test_forms_window_keys_hints(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(_map({_TICKERS: _json("tickers.json"), _AAPL: _json("aapl.json")})),
    )
    adapter = build(_settings(), _Redis(), _universe("AAPL", "SAP"))[0]
    result = asyncio.run(adapter.poll({}, None, None))
    eights = [
        item for item in result.events if item.source_key == "0000320193:8-K:2026-09-30"
    ]
    assert [item.form_type for item in eights] == ["8-K/A", "8-K"]
    assert eights[0].title.startswith("8-K/A — ")
    form4 = [item.source_key for item in result.events if ":4:" in item.source_key]
    assert len(set(form4)) == 2
    assert eights[0].entity_hints == [
        ("cik", "0000320193", "cik_map"),
        ("symbol", "AAPL", "cik_map"),
    ]
    by_key = {item.source_key: item for item in result.events}
    assert by_key["0000320193:SC 13D:0000320193-26-000030"].form_type == "SC 13D"
    assert by_key["0000320193:SC 13G:0000320193-26-000020"].form_type == "SCHEDULE 13G"
    assert all("10-Q" not in (item.form_type or "") for item in result.events)
    cursor = {"0000320193": "2026-10-01T16:30:00+00:00"}
    windowed = asyncio.run(adapter.poll(cursor, None, None))
    assert all(item.published_at.year == 2026 for item in windowed.events)
    assert all("2025-08-31" not in item.source_key for item in windowed.events)


def test_same_day_form4_distinct_keys(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(_map({_TICKERS: _json("tickers.json"), _AAPL: _json("aapl.json")})),
    )

    async def body(sessions):
        adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
        async with sessions() as session:
            stats = await ingest(session, adapter, NOW)
            assert stats.new >= 2
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(MarketEvent).where(MarketEvent.form_type == "4")
                    )
                )
                .scalars()
                .all()
            )
            keys = sorted(row.source_key for row in rows)
            assert keys == [
                "0000320193:4:0000320193-26-000040",
                "0000320193:4:0000320193-26-000041",
            ]
            assert {row.raw["accession"] for row in rows} == {
                "0000320193-26-000040",
                "0000320193-26-000041",
            }

    _with_db(body)


def test_8k_then_8ka_ingest_revision(monkeypatch):
    state = {"name": "aapl_8k.json"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        if url == _AAPL:
            return httpx.Response(200, content=_json(state["name"]))
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))

    async def body(sessions):
        adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
        async with sessions() as session:
            first = await ingest(session, adapter, NOW)
            assert first.new == 1
        state["name"] = "aapl_8ka.json"
        async with sessions() as session:
            second = await ingest(session, adapter, NOW)
            assert (second.revised, second.new) == (1, 0)
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(MarketEvent).order_by(MarketEvent.revision)
                    )
                )
                .scalars()
                .all()
            )
            assert [row.revision for row in rows] == [1, 2]
            assert rows[1].supersedes_id == rows[0].id
            assert rows[0].form_type == "8-K"
            assert rows[1].form_type == "8-K/A"

    _with_db(body)


def test_burst_including_tickers_enforces_limit(monkeypatch):
    hits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(time.monotonic())
        url = str(request.url)
        if url == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        if url == _AAPL:
            return httpx.Response(200, content=_json("aapl_8k.json"))
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(sec_edgar_rate_limit_per_sec=1), _Redis(), _universe("AAPL")
    )[0]
    asyncio.run(adapter.poll({}, None, None))
    assert len(hits) == 2
    assert hits[1] - hits[0] >= 0.9


def test_slow_transport_deadline_partial_cursor_only_completed(monkeypatch):
    original = httpx.AsyncClient.get
    delays = {_AAPL: 0.05, _MSFT: 5.0}

    async def wrapped(self, url, *args, **kwargs):
        wait = delays.get(str(url), 0.0)
        if wait:
            await asyncio.sleep(wait)
        return await original(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", wrapped)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(
            _map(
                {
                    _TICKERS: _json("tickers.json"),
                    _AAPL: _json("aapl_8k.json"),
                    _MSFT: _json("msft.json"),
                }
            )
        ),
    )
    adapter = build(
        _settings(events_adapter_timeout_seconds=11),
        _Redis(),
        _universe("AAPL", "MSFT"),
    )[0]
    started = time.monotonic()
    result = asyncio.run(adapter.poll({}, None, None))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert list(result.cursor) == ["0000320193"]
    assert result.events
    assert all("0789019" not in item.source_key for item in result.events)

    delays[_AAPL] = 5.0
    delays[_MSFT] = 0.05
    adapter = build(
        _settings(events_adapter_timeout_seconds=11),
        _Redis(),
        _universe("AAPL", "MSFT"),
    )[0]
    started = time.monotonic()
    first_slow = asyncio.run(adapter.poll({}, None, None))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert first_slow.cursor == {}
    assert first_slow.events == []


def test_no_cursor_newest_ten(monkeypatch):
    forms = ["4"] * 12
    payload = {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": [f"0000320193-26-{i:06d}" for i in range(12)],
                "filingDate": ["2026-10-01"] * 12,
                "reportDate": ["2026-09-30"] * 12,
                "acceptanceDateTime": [
                    f"2026-10-01T{12 + (i // 60):02d}:{i % 60:02d}:00.000Z"
                    for i in range(12)
                ],
                "form": forms,
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        return httpx.Response(200, content=json.dumps(payload).encode())

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
    result = asyncio.run(adapter.poll({}, None, None))
    assert len(result.events) == 10


def test_one_cik_connect_error_skipped(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        if url == _AAPL:
            raise httpx.ConnectError("down")
        if url == _MSFT:
            return httpx.Response(200, content=_json("msft.json"))
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), _Redis(), _universe("AAPL", "MSFT"))[0]
    result = asyncio.run(adapter.poll({}, None, None))
    assert list(result.cursor) == ["0000789019"]
    assert result.events[0].source_key.startswith("0000789019")


def test_three_consecutive_cik_failures_raise(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(),
        _Redis(),
        Universe(
            symbols=frozenset({"AAPL", "MSFT", "IBM"}),
            cik_map={"AAPL": "320193", "MSFT": "789019", "IBM": "51143"},
        ),
    )[0]
    with pytest.raises(httpx.TimeoutException):
        asyncio.run(adapter.poll({}, None, None))


def test_cik_map_preferred_and_missing_non_us_logged_once(monkeypatch, caplog):
    hits: list[str] = []
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(
            _map(
                {_TICKERS: _json("tickers.json"), _AAPL: _json("aapl_8k.json")},
                hits,
            )
        ),
    )
    caplog.set_level("INFO")
    adapter = build(
        _settings(),
        _Redis(),
        _universe("AAPL", "SAP", cik_map={"AAPL": "320193"}),
    )[0]
    asyncio.run(adapter.poll({}, None, None))
    asyncio.run(adapter.poll({}, None, None))
    assert hits.count(_AAPL) == 2
    assert sum("no CIK" in record.getMessage() for record in caplog.records) == 1


def test_conditional_etag_last_modified_304_no_cursor_loss(monkeypatch):
    redis = _Redis()
    headers = {
        _TICKERS: {
            "ETag": '"t-etag"',
            "Last-Modified": "Wed, 01 Oct 2025 12:00:00 GMT",
        },
        _AAPL: {
            "ETag": '"a-etag"',
            "Last-Modified": "Wed, 01 Oct 2025 13:00:00 GMT",
        },
        _MSFT: {
            "ETag": '"m-etag"',
            "Last-Modified": "Wed, 01 Oct 2025 14:00:00 GMT",
        },
    }
    bodies = {
        _TICKERS: _json("tickers.json"),
        _AAPL: _json("aapl_8k.json"),
        _MSFT: _json("msft.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        expected = headers.get(url)
        if expected is None:
            return httpx.Response(404)
        inm = request.headers.get("If-None-Match")
        ims = request.headers.get("If-Modified-Since")
        if inm or ims:
            if inm != expected["ETag"] or ims != expected["Last-Modified"]:
                raise AssertionError(f"cross-applied validators for {url}")
            return httpx.Response(304)
        return httpx.Response(200, content=bodies[url], headers=expected)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), redis, _universe("AAPL", "MSFT"))[0]
    first = asyncio.run(adapter.poll({}, None, None))
    assert first.events
    assert first.not_modified is False
    assert "0000320193" in first.cursor
    assert "0000789019" in first.cursor
    assert set(redis.expires.values()) == {86400}
    second = asyncio.run(
        adapter.poll(first.cursor, '"wrong"', "Thu, 01 Jan 1970 00:00:00 GMT")
    )
    # Note: 304 replays cached payload against the current cursor (2-day
    # overlap). Adapter events are no longer empty; ingest dedupes committed keys.
    assert second.not_modified is True
    assert {item.source_key for item in second.events} == {
        item.source_key for item in first.events
    }
    assert second.cursor == first.cursor


def test_default_rate_six_requests_rolling_window(monkeypatch):
    clock = {"t": 100.0}

    async def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr("app.adapters.events.edgar.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("app.adapters.events.edgar.asyncio.sleep", fake_sleep)
    hits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(clock["t"])
        if str(request.url) == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        return httpx.Response(200, content=_EMPTY)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    cik_map = {f"S{i}": str(1000 + i) for i in range(5)}
    adapter = build(
        _settings(),
        _Redis(),
        Universe(symbols=frozenset(cik_map), cik_map=cik_map),
    )[0]
    asyncio.run(adapter.poll({}, None, None))
    assert len(hits) == 6
    for stamp in hits:
        window = [item for item in hits if stamp <= item < stamp + 1.0]
        assert len(window) <= 5
    assert hits[-1] - hits[0] >= 1.0


def test_rate_wait_over_remaining_no_cursor_advance(monkeypatch):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        url = str(request.url)
        if url == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        if url == _AAPL:
            return httpx.Response(200, content=_json("aapl_8k.json"))
        if url == _MSFT:
            return httpx.Response(200, content=_json("msft.json"))
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(sec_edgar_rate_limit_per_sec=1, events_adapter_timeout_seconds=11),
        _Redis(),
        _universe("AAPL", "MSFT"),
    )[0]
    started = time.monotonic()
    result = asyncio.run(adapter.poll({}, None, None))
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert result.cursor == {}
    assert result.events == []
    assert hits == [_TICKERS]


@pytest.mark.parametrize("status", [403, 429])
def test_forbidden_and_rate_limit_raise(monkeypatch, status):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        return httpx.Response(status)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.poll({}, None, None))


def test_overlap_boundary_includes_exact_floor(monkeypatch):
    prev = "2026-10-01T16:30:00+00:00"
    payload = {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-26-000101",
                    "0000320193-26-000100",
                ],
                "filingDate": ["2026-09-29", "2026-09-29"],
                "reportDate": ["2026-09-29", "2026-09-29"],
                "acceptanceDateTime": [
                    "2026-09-29T16:30:00.000Z",
                    "2026-09-29T16:29:59.000Z",
                ],
                "form": ["4", "4"],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        return httpx.Response(200, content=json.dumps(payload).encode())

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
    result = asyncio.run(adapter.poll({"0000320193": prev}, None, None))
    keys = [item.source_key for item in result.events]
    assert keys == ["0000320193:4:0000320193-26-000101"]


def test_oldest_cursor_order_after_uncached(monkeypatch):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _TICKERS:
            return httpx.Response(200, content=_json("tickers.json"))
        hits.append(url)
        return httpx.Response(200, content=_EMPTY)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(
        _settings(),
        _Redis(),
        Universe(
            symbols=frozenset({"AAPL", "MSFT", "IBM"}),
            cik_map={"AAPL": "320193", "MSFT": "789019", "IBM": "51143"},
        ),
    )[0]
    asyncio.run(
        adapter.poll(
            {
                "0000320193": "2026-10-01T16:30:00+00:00",
                "0000051143": "2026-09-01T12:00:00+00:00",
            },
            None,
            None,
        )
    )
    assert hits == [_MSFT, _IBM, _AAPL]


def _etag_handler(
    bodies: dict[str, bytes], headers: dict[str, dict[str, str]], hits=None
):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if hits is not None:
            hits.append((url, request.headers.get("If-None-Match")))
        expected = headers.get(url)
        payload = bodies.get(url)
        if payload is None:
            return httpx.Response(404)
        if expected and (
            request.headers.get("If-None-Match")
            or request.headers.get("If-Modified-Since")
        ):
            return httpx.Response(304)
        return httpx.Response(200, content=payload, headers=expected or {})

    return handler


def test_malformed_recent_then_fixed_retry_not_304_empty(monkeypatch):
    state = {
        _AAPL: json.dumps(
            {
                "cik": "0000320193",
                "name": "Apple Inc.",
                "filings": {"recent": ["not", "a", "dict"]},
            }
        ).encode()
    }
    headers = {
        _TICKERS: {"ETag": '"t-etag"'},
        _AAPL: {"ETag": '"a-etag"'},
    }
    hits: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hits.append((url, request.headers.get("If-None-Match")))
        if url == _TICKERS:
            if request.headers.get("If-None-Match"):
                return httpx.Response(304)
            return httpx.Response(
                200, content=_json("tickers.json"), headers=headers[_TICKERS]
            )
        if url == _AAPL:
            if request.headers.get("If-None-Match"):
                return httpx.Response(304)
            return httpx.Response(200, content=state[_AAPL], headers=headers[_AAPL])
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
    with pytest.raises(AttributeError):
        asyncio.run(adapter.poll({}, None, None))
    state[_AAPL] = _json("aapl_8k.json")
    second = asyncio.run(adapter.poll({}, None, None))
    assert second.not_modified is False
    assert second.events
    assert all(item.source_key.startswith("0000320193") for item in second.events)
    assert (_AAPL, '"a-etag"') not in hits


def test_good_aapl_bad_msft_json_retry_304_replays(monkeypatch):
    msft_body = {"n": 0}
    headers = {
        _TICKERS: {"ETag": '"t-etag"'},
        _AAPL: {"ETag": '"a-etag"'},
        _MSFT: {"ETag": '"m-etag"'},
    }
    hits: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        inm = request.headers.get("If-None-Match")
        hits.append((url, inm))
        if url == _TICKERS:
            if inm:
                return httpx.Response(304)
            return httpx.Response(
                200, content=_json("tickers.json"), headers=headers[_TICKERS]
            )
        if url == _AAPL:
            if inm:
                return httpx.Response(304)
            return httpx.Response(
                200, content=_json("aapl_8k.json"), headers=headers[_AAPL]
            )
        if url == _MSFT:
            msft_body["n"] += 1
            if msft_body["n"] == 1:
                return httpx.Response(200, content=b"{")
            return httpx.Response(
                200, content=_json("msft.json"), headers=headers[_MSFT]
            )
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), _Redis(), _universe("AAPL", "MSFT"))[0]
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(adapter.poll({}, None, None))
    second = asyncio.run(adapter.poll({}, None, None))
    keys = {item.source_key for item in second.events}
    assert "0000320193:8-K:2026-09-30" in keys
    assert "0000789019:8-K:2026-09-30" in keys
    assert (_AAPL, '"a-etag"') in hits
    assert second.not_modified is False
    assert set(second.cursor) == {"0000320193", "0000789019"}


def test_stale_validator_without_payload_no_empty_map(monkeypatch):
    redis = _Redis()
    redis.store[_TICKERS_KEY] = json.dumps({"etag": '"t-etag"'})
    redis.store[_val_key(_TICKERS)] = json.dumps({"etag": '"t-etag"'})
    redis.store[_val_key(_AAPL)] = json.dumps({"etag": '"a-etag"'})
    hits: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        inm = request.headers.get("If-None-Match")
        hits.append((url, inm))
        if inm:
            return httpx.Response(304)
        if url == _TICKERS:
            return httpx.Response(
                200, content=_json("tickers.json"), headers={"ETag": '"t2"'}
            )
        if url == _AAPL:
            return httpx.Response(
                200, content=_json("aapl_8k.json"), headers={"ETag": '"a2"'}
            )
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), redis, _universe("AAPL"))[0]
    result = asyncio.run(adapter.poll({}, None, None))
    assert result.events
    assert result.not_modified is False
    assert (_TICKERS, None) in hits
    assert (_AAPL, None) in hits
    assert any(url == _AAPL for url, _inm in hits)


def test_bundle_ttl_atomic_and_mem_coherent(monkeypatch):
    headers = {
        _TICKERS: {
            "ETag": '"t-etag"',
            "Last-Modified": "Wed, 01 Oct 2025 12:00:00 GMT",
        },
        _AAPL: {"ETag": '"a-etag"', "Last-Modified": "Wed, 01 Oct 2025 13:00:00 GMT"},
    }
    bodies = {_TICKERS: _json("tickers.json"), _AAPL: _json("aapl_8k.json")}
    monkeypatch.setattr(httpx, "AsyncClient", _client(_etag_handler(bodies, headers)))
    redis = _Redis()
    adapter = build(_settings(), redis, _universe("AAPL"))[0]
    asyncio.run(adapter.poll({}, None, None))
    tickers = json.loads(redis.store[_TICKERS_KEY])
    submissions = json.loads(redis.store[_val_key(_AAPL)])
    assert "AAPL" in tickers["body"]
    assert tickers["etag"] == '"t-etag"'
    assert "filings" in submissions["body"]
    assert submissions["etag"] == '"a-etag"'
    assert redis.expires[_TICKERS_KEY] == 86400
    assert redis.expires[_val_key(_AAPL)] == 86400
    assert set(redis.expires.values()) == {86400}
    assert all("body" in json.loads(value) for value in redis.store.values())

    mem_adapter = build(_settings(), None, _universe("AAPL"))[0]
    asyncio.run(mem_adapter.poll({}, None, None))
    assert all(
        isinstance(value, dict) and "body" in value
        for value in mem_adapter._mem.values()
    )


def test_304_without_body_retries_unconditional(monkeypatch):
    redis = _Redis()
    redis.store[_TICKERS_KEY] = json.dumps({"body": {}, "etag": '"t-etag"'})
    hits: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.headers.get("If-None-Match"))
        return httpx.Response(304)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
    adapter = build(_settings(), redis, _universe("AAPL"))[0]
    result = asyncio.run(adapter.poll({}, None, None))
    assert result.events == []
    assert hits == [None, None]


def test_304_replay_ingest_dedupes_committed(monkeypatch):
    headers = {
        _TICKERS: {"ETag": '"t-etag"'},
        _AAPL: {"ETag": '"a-etag"'},
    }
    bodies = {_TICKERS: _json("tickers.json"), _AAPL: _json("aapl_8k.json")}
    monkeypatch.setattr(httpx, "AsyncClient", _client(_etag_handler(bodies, headers)))

    async def body(sessions):
        adapter = build(_settings(), _Redis(), _universe("AAPL"))[0]
        async with sessions() as session:
            first = await ingest(session, adapter, NOW)
            assert first.new == 1
        async with sessions() as session:
            second = await ingest(session, adapter, NOW)
            assert second.new == 0
            assert second.duplicate == 1
        async with sessions() as session:
            rows = (await session.execute(sa.select(MarketEvent))).scalars().all()
            assert len(rows) == 1
            assert rows[0].source_key == "0000320193:8-K:2026-09-30"

    _with_db(body)


def test_failed_poll_ingest_retry_replays_aapl_and_msft(monkeypatch):
    msft_body = {"n": 0}
    headers = {
        _TICKERS: {"ETag": '"t-etag"'},
        _AAPL: {"ETag": '"a-etag"'},
        _MSFT: {"ETag": '"m-etag"'},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        inm = request.headers.get("If-None-Match")
        if url == _TICKERS:
            if inm:
                return httpx.Response(304)
            return httpx.Response(
                200, content=_json("tickers.json"), headers=headers[_TICKERS]
            )
        if url == _AAPL:
            if inm:
                return httpx.Response(304)
            return httpx.Response(
                200, content=_json("aapl_8k.json"), headers=headers[_AAPL]
            )
        if url == _MSFT:
            msft_body["n"] += 1
            if msft_body["n"] == 1:
                return httpx.Response(200, content=b"{")
            return httpx.Response(
                200, content=_json("msft.json"), headers=headers[_MSFT]
            )
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _client(handler))

    async def body(sessions):
        adapter = build(_settings(), _Redis(), _universe("AAPL", "MSFT"))[0]
        async with sessions() as session:
            first = await ingest(session, adapter, NOW)
            assert first.new == 0
        later = NOW + timedelta(days=2)
        async with sessions() as session:
            second = await ingest(session, adapter, later)
            assert second.new == 2
            assert second.duplicate == 0
        async with sessions() as session:
            rows = (await session.execute(sa.select(MarketEvent))).scalars().all()
            keys = sorted(row.source_key for row in rows)
            assert keys == [
                "0000320193:8-K:2026-09-30",
                "0000789019:8-K:2026-09-30",
            ]

    _with_db(body)
