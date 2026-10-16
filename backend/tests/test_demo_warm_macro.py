"""Demo warm, macro step: demo-warm fetches public macro data through openbb-data
(MacroAdapter, live) and writes the `macro:*` keys the cache-only backend reads."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.adapters.macro_adapter import MacroAdapter
from app.config import get_settings
from tests.test_upstream_mode import _FakeRedis

_INDEX_TS = Path(__file__).resolve().parents[2] / "frontend/src/widgets/index.ts"


def test_macro_targets_cover_the_series_the_read_paths_request() -> None:
    import app.demo_warm as dw

    get_settings.cache_clear()
    s = get_settings()
    targets = set(dw.macro_series_targets(s))
    # market research: MacroAdapter.series(sid) for every FRED id, default transform
    assert {(sid, "toya") for sid in s.market_research_fred_list} <= targets
    if _INDEX_TS.exists():  # the macro-series widget's default series
        m = re.search(r'id:\s*"macro-series".*?defaultConfig:\s*\{\s*series:\s*"([^"]+)",\s*transform:\s*"([^"]+)"',
                      _INDEX_TS.read_text(), re.S)
        assert m and (m.group(1), m.group(2)) in targets


class _Resp:
    status_code = 200

    def __init__(self, body) -> None:  # noqa: ANN001
        self._body = body

    def json(self):  # noqa: ANN201
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Client:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *a) -> None:  # noqa: ANN002
        return None

    async def get(self, url, params=None):  # noqa: ANN001, ANN201
        return _Resp({"release_dates": [{"date": "2026-10-20", "release_name": "CPI"}]})


_ROWS = {
    "/fixedincome/government/yield_curve": [
        {"maturity": "year_10", "rate": 0.04, "date": "2026-10-15"},
        {"maturity": "year_2", "rate": 0.038, "date": "2026-10-15"},
        {"maturity": "month_3", "rate": 0.041, "date": "2026-10-15"}],
    "/fixedincome/government/treasury_rates": [
        {"date": "2026-10-14", "year_10": 0.04, "year_2": 0.038, "month_3": 0.041}],
    "/economy/indicators": [{"country": "United States", "date": "2026-09-01", "value": 0.03}],
    "/economy/fred_series": [{"date": "2026-10-01", "value": 1.5}],
}


@pytest.fixture
def macro_env(monkeypatch: pytest.MonkeyPatch):
    """Fake openbb-data rows + FRED calendar; yields (demo_warm, redis, settings(key))."""
    import app.demo_warm as dw

    async def _results(self, client, path, params):  # noqa: ANN001
        return _ROWS[path]

    async def _ready(settings):  # noqa: ANN001
        return None

    monkeypatch.setattr(MacroAdapter, "_results", _results)
    monkeypatch.setattr(MacroAdapter, "_client", lambda self, timeout=None: _Client())
    monkeypatch.setattr(dw, "_macro_ready", _ready)
    monkeypatch.setattr(dw, "MACRO_RETRY_WAIT_SECONDS", 0.0)
    monkeypatch.setenv("UPSTREAM_MODE", "live")

    def settings(key: str | None):  # noqa: ANN202
        if key:
            monkeypatch.setenv("OPENBB_FRED_API_KEY", key)
        else:
            monkeypatch.delenv("OPENBB_FRED_API_KEY", raising=False)
        get_settings.cache_clear()
        return get_settings()

    yield dw, _FakeRedis(), settings
    get_settings.cache_clear()


async def _run_macro(dw, redis, settings):  # noqa: ANN001, ANN202
    failed: list[str] = []

    async def step(label, coro, core=False):  # noqa: ANN001
        try:
            await coro
        except Exception as exc:
            failed.append(f"{label}: {exc}")

    await dw.warm_macro(redis, settings, step)
    return failed


async def test_warm_with_key_fills_every_key_the_cache_only_reads_use(macro_env, monkeypatch: pytest.MonkeyPatch) -> None:
    dw, redis, settings = macro_env
    assert await _run_macro(dw, redis, settings("demo-key")) == []
    assert all(f"{k}:stale" in redis.kv for k in redis.kv if not k.endswith(":stale"))  # KEY + KEY:stale

    # Now as the demo backend: cache-only, no FRED key, every macro read path.
    monkeypatch.setenv("UPSTREAM_MODE", "cache-only")
    monkeypatch.delenv("OPENBB_FRED_API_KEY", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    a = MacroAdapter(redis, s)
    reads = {
        "yc:US": await a.yield_curve("US"), "yc:EU": await a.yield_curve("EU"),
        "rates": await a.rates(), "indicators": await a.indicators(), "calendar": await a.calendar(),
    }
    for sid, transform in dw.macro_series_targets(s):
        reads[f"series:{sid}"] = await a.series(sid, transform)
    for name, env in reads.items():
        assert not env.stale and not dw._macro_empty(env.data), name
    assert reads["calendar"].data.needs_key is False  # warmed calendar served despite no key


async def test_warm_without_key_warms_the_keyless_parts_and_skips_fred_only(macro_env, caplog: pytest.LogCaptureFixture) -> None:
    dw, redis, settings = macro_env
    with caplog.at_level("WARNING", logger="catallax.demo_warm"):
        assert await _run_macro(dw, redis, settings(None)) == []  # skips are warnings, not failures
    assert {"macro:yc:US", "macro:yc:EU", "macro:rates:US", "macro:indicators",
            "macro:series:CPI.united_states:toya"} <= set(redis.kv)
    assert not any(k.startswith(("macro:series:VIXCLS", "macro:calendar")) for k in redis.kv)
    assert "calendar skipped" in caplog.text and "series VIXCLS skipped" in caplog.text


async def test_sidecar_down_is_one_optional_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.demo_warm as dw

    monkeypatch.setenv("OPENBB_API_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(dw, "MACRO_READY_WAIT_SECONDS", 0.0)
    get_settings.cache_clear()
    failed = await _run_macro(dw, _FakeRedis(), get_settings())
    assert len(failed) == 1 and failed[0].startswith("macro ready")
    get_settings.cache_clear()


async def test_empty_upstream_result_is_a_failed_step(macro_env, monkeypatch: pytest.MonkeyPatch) -> None:
    dw, redis, settings = macro_env

    async def _none(self, client, path, params):  # noqa: ANN001
        return []

    monkeypatch.setattr(MacroAdapter, "_results", _none)
    failed = await _run_macro(dw, redis, settings(None))
    assert any("yield-curve US" in f for f in failed) and not any(k.startswith("macro:") for k in redis.kv)


async def test_a_transient_empty_answer_is_retried(macro_env, monkeypatch: pytest.MonkeyPatch) -> None:
    dw, redis, settings = macro_env
    calls = {"n": 0}

    async def _flaky(self, client, path, params):  # noqa: ANN001
        calls["n"] += 1
        return [] if calls["n"] == 1 else _ROWS[path]

    monkeypatch.setattr(MacroAdapter, "_results", _flaky)
    assert await _run_macro(dw, redis, settings(None)) == []
    assert "macro:yc:US" in redis.kv


def _calendar_with_real_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """MacroAdapter.calendar() through a real httpx client on a mock transport (httpx logs the URL)."""
    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"release_dates": [{"date": "2026-10-20", "release_name": "CPI"}]})

    monkeypatch.setattr(MacroAdapter, "_client",
                        lambda self, timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))


@pytest.fixture
def httpx_loggers():
    import logging

    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


async def test_the_fred_key_never_reaches_the_logs(macro_env, monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture, httpx_loggers) -> None:
    import logging

    dw, redis, settings = macro_env
    key = "SECRETFREDKEY0123456789abcdef"
    _calendar_with_real_httpx(monkeypatch)
    monkeypatch.setattr(dw, "macro_series_targets", lambda s: [])
    # control: with httpx at INFO (what basicConfig(INFO) gives) the URL, key included, is logged
    logging.getLogger("httpx").setLevel(logging.INFO)
    with caplog.at_level(logging.DEBUG):
        await _run_macro(dw, redis, settings(key))
    assert key in caplog.text
    # the job's logging setup must silence it
    caplog.clear()
    redis.kv.clear()
    dw._configure_logging()
    with caplog.at_level(logging.DEBUG):
        failed = await _run_macro(dw, redis, settings(key))
    assert failed == [] and "macro:calendar" in redis.kv
    assert key not in caplog.text
