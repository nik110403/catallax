"""Demo macro handoff: allowlist, validation, round trip through the cache-only adapter,
and the calendar() cache-before-key-check fix."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.adapters.macro_adapter import (
    CalendarData, CalendarEvent, CurvePoint, IndicatorBoard, IndicatorCell, MacroAdapter,
    MacroPoint, MacroSeries, RateRow, RatesData, SpreadPoint, YieldCurve,
)
from app.config import Settings
from app.macro_import import KEY_RE, import_macro
from app.upstream import UpstreamDisabled


class _Redis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ex: dict[str, int | None] = {}

    async def get(self, key: str):
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value
        self.ex[key] = ex


def _settings(**kw: object) -> Settings:
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r",
                    upstream_mode="cache-only", **kw)


GOOD = {
    "macro:yc:US": YieldCurve(region="US", date="2026-10-15", points=[CurvePoint(maturity="2Y", years=2, rate=4.0)],
                              spread_10y_2y=0.1, spread_10y_3m=0.2, inverted=False),
    "macro:rates:US": RatesData(latest=[RateRow(key="fed", label="Fed", value=4.5, date=None)],
                                spreads=[SpreadPoint(date="2026-10-15", t10y_2y=0.1, t10y_3m=0.2)]),
    "macro:indicators": IndicatorBoard(countries=["US"], cells=[IndicatorCell(
        root="CPI", label="CPI", country="US", value=0.02, date=None, transform="toya")]),
    "macro:calendar": CalendarData(events=[CalendarEvent(date="2026-10-20", country="US", event="CPI",
                                                         actual=None, previous=None, consensus=None)], needs_key=False),
    "macro:series:VIXCLS:toya": MacroSeries(series_id="VIXCLS", title="VIX", unit="level",
                                            points=[MacroPoint(date="2026-10-15", value=15.0)]),
    "macro:series:CPI.united_states:toya": MacroSeries(series_id="CPI.united_states", title="CPI", unit="% YoY",
                                                       points=[MacroPoint(date="2026-09-01", value=0.03)]),
}


def _file(tmp_path: Path, entries: dict, *, version: int = 1, age: timedelta = timedelta(hours=1)) -> str:
    doc = {"version": version, "generated_at": (datetime.now(timezone.utc) - age).isoformat(),
           "entries": {k: (v.model_dump_json() if hasattr(v, "model_dump_json") else v) for k, v in entries.items()}}
    p = tmp_path / "macro.json"
    p.write_text(json.dumps(doc))
    return str(p)


@pytest.mark.parametrize("key", [
    "risk:summary", "macro:series:X:other", "macro:series::toya", "macro:yc:JP", "macro:rates:EU",
    "macro:calendar:stale", "macro:series:A b:toya", "macro:series:A\n:toya", "macro:*", "xmacro:calendar",
    "macro:series:../x:toya", "macro:series:X:toya\n",
])
def test_allowlist_rejects(key: str) -> None:
    assert not KEY_RE.fullmatch(key)


@pytest.mark.parametrize("key", ["macro:yc:US", "macro:yc:EU", "macro:rates:US", "macro:indicators",
                                 "macro:calendar", "macro:series:CPI.united_states:toya", "macro:series:T10Y2Y:level"])
def test_allowlist_accepts(key: str) -> None:
    assert KEY_RE.fullmatch(key)


async def test_round_trip_serves_cache_only_adapter(tmp_path: Path) -> None:
    r = _Redis()
    n = await import_macro(r, 21600, _file(tmp_path, GOOD))
    assert n == len(GOOD)
    assert r.ex["macro:yc:US"] == 21600 and r.ex["macro:yc:US:stale"] == 7 * 86400
    s = _settings()
    a = MacroAdapter(r, s)
    assert (await a.yield_curve("US")).data.points[0].rate == 4.0
    assert (await a.rates()).data.latest[0].value == 4.5
    assert (await a.series("VIXCLS", "toya")).data.points[0].value == 15.0
    assert (await a.series("CPI.united_states", "toya")).data.series_id == "CPI.united_states"
    cal = await a.calendar()
    assert cal.data.events and not cal.data.needs_key  # no FRED key configured, still served
    # without import the same reads are refused in cache-only mode
    with pytest.raises(UpstreamDisabled):
        await MacroAdapter(_Redis(), s).yield_curve("US")


async def test_stale_copy_serves_after_fresh_key_expires(tmp_path: Path) -> None:
    r = _Redis()
    await import_macro(r, 21600, _file(tmp_path, {"macro:calendar": GOOD["macro:calendar"]}))
    del r.kv["macro:calendar"]
    cal = await MacroAdapter(r, _settings()).calendar()
    assert cal.data.events and cal.stale


async def test_calendar_without_cache_or_key_still_needs_key() -> None:
    cal = await MacroAdapter(_Redis(), _settings()).calendar()
    assert cal.data.needs_key and not cal.data.events


async def test_bad_entries_are_skipped_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    r = _Redis()
    entries = {
        "macro:yc:US": GOOD["macro:yc:US"],
        "risk:summary": "{}",
        "macro:series:X:other": GOOD["macro:series:VIXCLS:toya"],
        "macro:series:a'; DEL *:toya": GOOD["macro:series:VIXCLS:toya"],
        "macro:yc:EU": '{"region": 1, "nope": true}',
        "macro:indicators": "not json",
        "macro:rates:US": RatesData(latest=[], spreads=[]),  # empty: not cached
    }
    with caplog.at_level(logging.WARNING, logger="catallax.macro_import"):
        n = await import_macro(r, 60, _file(tmp_path, entries))
    assert n == 1 and set(r.kv) == {"macro:yc:US", "macro:yc:US:stale"}
    assert len([x for x in caplog.records if x.levelno == logging.WARNING]) == 6


async def test_object_values_are_accepted(tmp_path: Path) -> None:
    r = _Redis()
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                             "entries": {"macro:calendar": GOOD["macro:calendar"].model_dump()}}))
    assert await import_macro(r, 60, str(p)) == 1


async def test_file_level_rejections(tmp_path: Path) -> None:
    r = _Redis()
    assert await import_macro(r, 60, str(tmp_path / "missing.json")) == 0
    assert await import_macro(r, 60, None) == 0
    assert await import_macro(r, 60, _file(tmp_path, GOOD, version=2)) == 0
    (tmp_path / "bad.json").write_text("{not json")
    assert await import_macro(r, 60, str(tmp_path / "bad.json")) == 0
    many = {f"macro:series:S{i}:toya": "{}" for i in range(501)}
    assert await import_macro(r, 60, _file(tmp_path, many)) == 0
    big = tmp_path / "big.json"
    big.write_text(" " * (5 * 1024 * 1024 + 1))
    assert await import_macro(r, 60, str(big)) == 0
    assert not r.kv


async def test_old_file_warns_but_imports(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    r = _Redis()
    with caplog.at_level(logging.WARNING, logger="catallax.macro_import"):
        n = await import_macro(r, 60, _file(tmp_path, GOOD, age=timedelta(hours=49)))
    assert n == len(GOOD) and any("stale" in x.getMessage() for x in caplog.records)
    # an old export never lands under the fresh key: the UI must show the stale label
    assert not any(not k.endswith(":stale") for k in r.kv) and "macro:yc:US:stale" in r.kv
    cal = await MacroAdapter(r, _settings()).calendar()
    assert cal.stale


async def test_deep_json_and_bad_ttl_do_not_crash(tmp_path: Path) -> None:
    r = _Redis()
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 200000)
    assert await import_macro(r, 60, str(deep)) == 0
    await import_macro(r, 0, _file(tmp_path, {"macro:calendar": GOOD["macro:calendar"]}))
    assert "macro:calendar" not in r.kv and "macro:calendar:stale" in r.kv


def test_series_id_is_length_bounded() -> None:
    assert KEY_RE.fullmatch("macro:series:" + "A" * 64 + ":toya")
    assert not KEY_RE.fullmatch("macro:series:" + "A" * 65 + ":toya")


async def test_calendar_survives_a_redis_read_error() -> None:
    class _Boom:
        async def get(self, key):  # noqa: ANN001
            raise ConnectionError("down")

    cal = await MacroAdapter(_Boom(), _settings()).calendar()
    assert cal.data.needs_key


async def test_warm_step_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing DEMO_MACRO_IMPORT file never counts as a failure in the warm."""
    import app.demo_warm as dw

    monkeypatch.setenv("DEMO_MACRO_IMPORT", str(tmp_path / "nope.json"))
    assert await dw.import_macro(_Redis(), 60, str(tmp_path / "nope.json")) == 0
