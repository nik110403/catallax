"""MacroAdapter — the seam to the OpenBB `openbb-data` sidecar.

OpenBB Platform's REST API runs as an isolated compose service (internal network
only, no host port); this adapter is the *only* thing that talks to it — same
sidecar pattern as the Fava / Wealthfolio bridges. Route handlers depend on
`get_macro_adapter`; the frontend reaches macro data only through `/api/v1/macro/*`.

Free providers only:
  * `federal_reserve` — US treasury rates + yield curve (keyless)
  * `ecb`             — euro-area yield curve (keyless)
  * `econdb`          — multi-country macro indicators, YoY transform (keyless)
  * `fred`            — arbitrary FRED series, CPI, the release calendar (FREE key
                        via `OPENBB_FRED_API_KEY`; keyless paths work without it)

Every method returns an `Envelope[T]` so the UI gets the provider + freshness
alongside the data (provenance dots). Macro data moves daily at most, so results
are cached hard in Redis (`macro_cache_ttl_seconds`, hours).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import Depends
from pydantic import BaseModel
from redis.asyncio import Redis

from app.adapters.envelope import Envelope
from app.config import Settings, get_settings
from app.errors import SidecarNotEnabled, UpstreamError
from app.redis import get_redis
from app.upstream import guard, is_cache_only

logger = logging.getLogger("catallax.macro")

# Standard maturity labels OpenBB uses for both treasury_rates columns and
# yield_curve point labels (federal_reserve + ecb share the scheme).
_LABEL_YEARS: dict[str, float] = {
    "month_1": 1 / 12, "month_2": 2 / 12, "month_3": 0.25, "month_6": 0.5,
    "year_1": 1, "year_2": 2, "year_3": 3, "year_5": 5, "year_7": 7,
    "year_10": 10, "year_20": 20, "year_30": 30,
}
_PRETTY_MATURITY: dict[str, str] = {
    "month_1": "1M", "month_2": "2M", "month_3": "3M", "month_6": "6M",
    "year_1": "1Y", "year_2": "2Y", "year_3": "3Y", "year_5": "5Y",
    "year_7": "7Y", "year_10": "10Y", "year_20": "20Y", "year_30": "30Y",
}
_INDICATOR_LABELS: dict[str, str] = {
    "CPI": "CPI", "CORE": "Core CPI", "PPI": "PPI",
    "URATE": "Unemployment", "RGDP": "Real GDP", "GDP": "GDP", "POP": "Population",
}


# ── Response models ────────────────────────────────────────────
class MacroPoint(BaseModel):
    date: str
    value: float


class MacroSeries(BaseModel):
    series_id: str
    title: str
    unit: str  # "% YoY" | "%" | "level"
    points: list[MacroPoint]


class IndicatorCell(BaseModel):
    root: str
    label: str
    country: str
    value: float | None  # fraction (0.0198 = 1.98%); UI multiplies by 100
    date: str | None
    transform: str  # "toya" (YoY) | "level"


class IndicatorBoard(BaseModel):
    countries: list[str]  # display names, column order
    cells: list[IndicatorCell]


class CurvePoint(BaseModel):
    maturity: str  # pretty: "3M" / "2Y" / "10Y"
    years: float
    rate: float  # percent


class YieldCurve(BaseModel):
    region: str  # US | EU
    date: str | None
    points: list[CurvePoint]
    spread_10y_2y: float | None
    spread_10y_3m: float | None
    inverted: bool


class SpreadPoint(BaseModel):
    date: str
    t10y_2y: float | None
    t10y_3m: float | None


class RateRow(BaseModel):
    key: str
    label: str
    value: float | None  # percent
    date: str | None


class RatesData(BaseModel):
    latest: list[RateRow]
    spreads: list[SpreadPoint]


class CalendarEvent(BaseModel):
    date: str | None
    country: str | None
    event: str
    actual: str | None
    previous: str | None
    consensus: str | None


class CalendarData(BaseModel):
    events: list[CalendarEvent]
    needs_key: bool  # true when no FRED key is configured (calendar is FRED-only)


_STALE_WARN = "live source returned no data; showing the last good result"


def _as_pct(v: Any) -> float | None:
    """OpenBB rates arrive as fractions (0.0438) → percent (4.38)."""
    try:
        return round(float(v) * 100, 4)
    except (TypeError, ValueError):
        return None


class MacroAdapter:
    """HTTP client to the openbb-data REST API, Redis-cached, Envelope-wrapping."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.base = settings.openbb_api_url.rstrip("/")
        self.ttl = settings.macro_cache_ttl_seconds

    # ── low-level OpenBB call ──────────────────────────────────
    async def _results(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        try:
            resp = await client.get(f"{self.base}/api/v1{path}", params=params)
        except httpx.ConnectError as exc:
            # Nothing listening at all (refused/no route/DNS) — almost always
            # means the `macro` profile was never started, not a live-but-broken
            # sidecar. A read timeout on an established connection is NOT this;
            # that stays UpstreamError below.
            raise SidecarNotEnabled("openbb-data", "macro") from exc
        except httpx.ConnectTimeout as exc:
            # A RUNNING sidecar behind a firewall DROP also raises
            # ConnectTimeout (hit exactly this during a host migration —
            # see the Fava 5000->5001 comment in docker-compose.yml), so
            # telling the operator "it isn't running" would misdirect them.
            # Only an outright refused/no-route connection (ConnectError,
            # above) is SidecarNotEnabled; a timed-out connect is an upstream
            # failure of a sidecar that may well be up (security review,
            # 2026-08-15).
            raise UpstreamError(f"openbb-data connect timed out for {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"openbb-data unreachable for {path}: {exc}") from exc
        if resp.status_code == 204:
            return []
        if resp.status_code >= 400:
            # OpenBB validation/credential errors carry a `detail`; log it (it may
            # name a provider/key), surface a generic upstream error to the client.
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text[:200]
            logger.warning("openbb %s -> %s: %s", path, resp.status_code, detail)
            raise UpstreamError(f"OpenBB request failed for {path}")
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else None
        return results or []

    # ── Redis cache wrapper with stale fallback ────────────────
    # Returns (model, cache_hit, stale). Empty results are NOT cached as good
    # (so a transient upstream blank doesn't poison the TTL); instead the last
    # good result is served from a long-lived stale key (the markets-crypto
    # pattern). econdb in particular 204s under load when unauthenticated.
    _STALE_TTL = 7 * 86400

    async def _cached(
        self, cache_key: str, model: type[BaseModel], builder: Any, is_empty: Any
    ) -> tuple[BaseModel, bool, bool]:
        raw = await self.redis.get(cache_key)
        if raw is not None:
            try:
                return model.model_validate_json(raw), True, False
            except Exception as exc:
                logger.debug("macro cache deserialise failed (%s): %s", cache_key, exc)
        if is_cache_only(self.settings):
            # Demo: no upstream on a miss — serve the last good result or fail.
            stale = await self.redis.get(f"{cache_key}:stale")
            if stale is not None:
                try:
                    return model.model_validate_json(stale), False, True
                except Exception as exc:
                    logger.debug("macro stale deserialise failed (%s): %s", cache_key, exc)
            guard("macro", self.settings)
        data = await builder()
        if not is_empty(data):
            try:
                payload = data.model_dump_json()
                await self.redis.set(cache_key, payload, ex=self.ttl)
                await self.redis.set(f"{cache_key}:stale", payload, ex=self._STALE_TTL)
            except Exception as exc:  # caching is best-effort
                logger.warning("macro cache set failed (%s): %s", cache_key, exc)
            return data, False, False
        # Upstream returned nothing usable — serve the last good result if we have one.
        stale = await self.redis.get(f"{cache_key}:stale")
        if stale is not None:
            try:
                return model.model_validate_json(stale), False, True
            except Exception as exc:
                logger.debug("macro stale deserialise failed (%s): %s", cache_key, exc)
        return data, False, False

    def _client(self) -> httpx.AsyncClient:
        guard("macro", self.settings)
        return httpx.AsyncClient(timeout=self.settings.openbb_http_timeout)

    # ── public API (each returns Envelope[Model]) ──────────────
    async def yield_curve(self, region: str) -> Envelope[YieldCurve]:
        region = region.upper()
        provider = "ecb" if region == "EU" else "federal_reserve"

        async def build() -> YieldCurve:
            async with self._client() as client:
                rows = await self._results(
                    client, "/fixedincome/government/yield_curve", {"provider": provider}
                )
            # OpenBB returns the latest curve as one point per maturity.
            pts: list[CurvePoint] = []
            for r in rows:
                label = r.get("maturity")
                rate = _as_pct(r.get("rate"))
                if rate is None:
                    continue
                years = r.get("maturity_years") or _LABEL_YEARS.get(label, 0.0)
                pts.append(
                    CurvePoint(
                        maturity=_PRETTY_MATURITY.get(label, label or "?"),
                        years=float(years),
                        rate=rate,
                    )
                )
            pts.sort(key=lambda p: p.years)
            date = rows[0].get("date") if rows else None
            by_years = {round(p.years, 4): p.rate for p in pts}

            def at(y: float) -> float | None:
                # nearest maturity within a small tolerance of the target tenor
                best = min(by_years, key=lambda k: abs(k - y), default=None)
                return by_years[best] if best is not None and abs(best - y) <= 0.6 else None

            r10, r2, r3m = at(10), at(2), at(0.25)
            s_10_2 = round(r10 - r2, 4) if r10 is not None and r2 is not None else None
            s_10_3m = round(r10 - r3m, 4) if r10 is not None and r3m is not None else None
            return YieldCurve(
                region=region, date=date, points=pts,
                spread_10y_2y=s_10_2, spread_10y_3m=s_10_3m,
                inverted=bool(s_10_2 is not None and s_10_2 < 0),
            )

        data, hit, stale = await self._cached(
            f"macro:yc:{region}", YieldCurve, build, lambda d: not d.points
        )
        return Envelope.fresh(
            data, source=provider, cache_hit=hit, stale=stale,
            warnings=[_STALE_WARN] if stale else [],
        )

    async def rates(self) -> Envelope[RatesData]:
        """US Treasury constant-maturity snapshot + the recession spread history."""

        async def build() -> RatesData:
            async with self._client() as client:
                rows = await self._results(
                    client, "/fixedincome/government/treasury_rates",
                    {"provider": "federal_reserve"},
                )
            rows = [r for r in rows if r.get("date")]
            rows.sort(key=lambda r: r["date"])
            spreads: list[SpreadPoint] = []
            for r in rows:
                y10, y2, m3 = _as_pct(r.get("year_10")), _as_pct(r.get("year_2")), _as_pct(r.get("month_3"))
                spreads.append(
                    SpreadPoint(
                        date=r["date"],
                        t10y_2y=round(y10 - y2, 4) if y10 is not None and y2 is not None else None,
                        t10y_3m=round(y10 - m3, 4) if y10 is not None and m3 is not None else None,
                    )
                )
            latest_row = rows[-1] if rows else {}
            latest: list[RateRow] = []
            for label in ("month_3", "year_2", "year_5", "year_10", "year_30"):
                latest.append(
                    RateRow(
                        key=label,
                        label=_PRETTY_MATURITY.get(label, label),
                        value=_as_pct(latest_row.get(label)),
                        date=latest_row.get("date"),
                    )
                )
            return RatesData(latest=latest, spreads=spreads[-520:])  # ~2y of daily spreads

        data, hit, stale = await self._cached(
            "macro:rates:US", RatesData, build, lambda d: not d.spreads
        )
        return Envelope.fresh(
            data, source="federal_reserve", cache_hit=hit, stale=stale,
            warnings=[_STALE_WARN] if stale else [],
        )

    async def indicators(self) -> Envelope[IndicatorBoard]:
        countries = self.settings.macro_country_list

        async def build() -> IndicatorBoard:
            cells: list[IndicatorCell] = []
            seen_countries: list[str] = []
            async with self._client() as client:
                for root, transform in self.settings.macro_indicators:
                    params: dict[str, Any] = {
                        "provider": "econdb",
                        "symbol": root,
                        "country": ",".join(countries),
                    }
                    if transform != "level":
                        params["transform"] = transform
                    try:
                        rows = await self._results(client, "/economy/indicators", params)
                    except UpstreamError as exc:
                        # one bad root shouldn't sink the whole board, but log it
                        logger.warning("macro indicator root %s (%s) failed: %s", root, transform, exc)
                        rows = []
                    # latest value per country for this root
                    latest: dict[str, dict[str, Any]] = {}
                    for r in rows:
                        c = r.get("country")
                        if not c:
                            continue
                        if c not in latest or (r.get("date") or "") > (latest[c].get("date") or ""):
                            latest[c] = r
                    for c, r in latest.items():
                        if c not in seen_countries:
                            seen_countries.append(c)
                        cells.append(
                            IndicatorCell(
                                root=root,
                                label=_INDICATOR_LABELS.get(root, root),
                                country=c,
                                value=(float(r["value"]) if r.get("value") is not None else None),
                                date=r.get("date"),
                                transform=transform,
                            )
                        )
            return IndicatorBoard(countries=seen_countries, cells=cells)

        data, hit, stale = await self._cached(
            "macro:indicators", IndicatorBoard, build, lambda d: not d.cells
        )
        return Envelope.fresh(
            data, source="econdb", cache_hit=hit, stale=stale,
            warnings=[_STALE_WARN] if stale else [],
        )

    async def series(self, series_id: str, transform: str = "toya") -> Envelope[MacroSeries]:
        """A single macro time series.

        `series_id` of the form `ROOT.country` (e.g. `CPI.united_states`) is served
        keyless by econdb; a bare id (e.g. `DGS10`, `UNRATE`) is a FRED series and
        needs `OPENBB_FRED_API_KEY`.
        """
        is_fred = "." not in series_id
        provider = "fred" if is_fred else "econdb"

        async def build() -> MacroSeries:
            async with self._client() as client:
                if is_fred:
                    if not self.settings.openbb_fred_api_key:
                        return MacroSeries(
                            series_id=series_id,
                            title=f"{series_id} (set OPENBB_FRED_API_KEY)",
                            unit="level", points=[],
                        )
                    rows = await self._results(
                        client, "/economy/fred_series", {"provider": "fred", "symbol": series_id}
                    )
                    # OpenBB fred_series may key the value by the symbol or by "value"
                    # (shape unverified here — gated behind the FRED key). Handle both.
                    pts: list[MacroPoint] = []
                    for r in rows:
                        if r.get("date") is None:
                            continue
                        val = r.get(series_id)
                        if val is None:
                            val = r.get("value")
                        if val is None:
                            continue
                        try:
                            pts.append(MacroPoint(date=str(r["date"]), value=float(val)))
                        except (TypeError, ValueError):
                            continue
                    return MacroSeries(series_id=series_id, title=series_id, unit="level", points=pts)

                root, _, country = series_id.partition(".")
                root = root.upper()
                # econdb yields data only for a multi-country request, so ask for the
                # full set and filter to the requested country (case-insensitive).
                wanted = country.replace("_", " ").lower()
                cset = self.settings.macro_country_list
                if country not in cset:
                    cset = [country, *cset]
                params: dict[str, Any] = {
                    "provider": "econdb", "symbol": root, "country": ",".join(cset),
                }
                if transform != "level":
                    params["transform"] = transform
                rows = await self._results(client, "/economy/indicators", params)
                pts = [
                    MacroPoint(date=str(r.get("date")), value=float(r["value"]))
                    for r in rows
                    if (r.get("country") or "").lower() == wanted and r.get("value") is not None
                ]
                pts.sort(key=lambda p: p.date)
                unit = "% YoY" if transform == "toya" else "%"
                title = f"{_INDICATOR_LABELS.get(root, root)} · {country.replace('_', ' ').title()}"
                return MacroSeries(series_id=series_id, title=title, unit=unit, points=pts)

        key = f"macro:series:{series_id}:{transform}"
        data, hit, stale = await self._cached(key, MacroSeries, build, lambda d: not d.points)
        return Envelope.fresh(
            data, source=provider, cache_hit=hit, stale=stale,
            warnings=[_STALE_WARN] if stale else [],
        )

    async def calendar(self, days: int = 14) -> Envelope[CalendarData]:
        """Economic release calendar — direct FRED `releases/dates` (free key).

        Deliberately NOT via openbb-data: OpenBB's fred calendar provider fans
        out one request per release and reliably times out (silent empty
        widget). The native endpoint returns all upcoming release dates in a
        single call. Still behind this adapter — the key never leaves the
        backend. Names/dates only (FRED publishes no consensus/actual here).
        """
        if not self.settings.openbb_fred_api_key:
            # No key can still mean a warmed calendar (the cache-only demo backend never holds one): serve it first.
            for suffix, stale in (("", False), (":stale", True)):
                try:
                    raw = await self.redis.get(f"macro:calendar{suffix}")
                except Exception as exc:
                    logger.debug("macro calendar cache read failed: %s", exc)
                    break
                if raw is None:
                    continue
                try:
                    cached = CalendarData.model_validate_json(raw)
                except Exception as exc:
                    logger.debug("macro calendar cache deserialise failed: %s", exc)
                    continue
                if cached.events:
                    return Envelope.fresh(
                        cached, source="fred", cache_hit=not stale, stale=stale,
                        warnings=[_STALE_WARN] if stale else [],
                    )
            data = CalendarData(events=[], needs_key=True)
            return Envelope.fresh(
                data, source="fred", stale=True,
                warnings=["Economic calendar needs a free FRED API key (OPENBB_FRED_API_KEY)."],
            )

        failed = False

        async def build() -> CalendarData:
            nonlocal failed
            today = datetime.now(timezone.utc).date()
            params = {
                "api_key": self.settings.openbb_fred_api_key,
                "file_type": "json",
                "realtime_start": today.isoformat(),
                "realtime_end": (today + timedelta(days=days)).isoformat(),
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
                "limit": 500,
            }
            rows: list[dict[str, Any]] = []
            try:
                async with self._client() as client:
                    resp = await client.get(
                        self.settings.fred_releases_dates_url, params=params
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    rows = body.get("release_dates") or [] if isinstance(body, dict) else []
            except Exception as exc:
                failed = True
                # httpx exception text includes the full URL (and the key) — sanitize.
                logger.warning(
                    "fred releases/dates failed: %s",
                    re.sub(r"api_key=[^&\s]+", "api_key=***", str(exc)),
                )
            events: list[CalendarEvent] = []
            seen: set[tuple[str | None, str]] = set()
            for r in rows:
                d = _str(r.get("date"))
                name = _str(r.get("release_name")) or "—"
                if (d, name) in seen:
                    continue
                seen.add((d, name))
                events.append(
                    CalendarEvent(
                        date=d, country="US", event=name,
                        actual=None, previous=None, consensus=None,
                    )
                )
            return CalendarData(events=events, needs_key=False)

        data, hit, stale = await self._cached(
            "macro:calendar", CalendarData, build, lambda d: not d.events
        )
        warnings = [_STALE_WARN] if stale else []
        if failed and not data.events:
            warnings.append(
                "Economic calendar source unavailable (FRED releases/dates request failed)."
            )
        return Envelope.fresh(
            data, source="fred", cache_hit=hit, stale=stale, warnings=warnings,
        )


def _str(v: Any) -> str | None:
    return None if v is None else str(v)


def get_macro_adapter(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> MacroAdapter:
    return MacroAdapter(redis, settings)
