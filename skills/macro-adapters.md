# Macro Adapter (Catallax, Phase 8)

All macro/economic data sits behind `MacroAdapter`
(`backend/app/adapters/macro_adapter.py`), the **only** thing that talks to the
OpenBB `openbb-data` sidecar. Frontend → `/api/v1/macro/*` → `MacroAdapter` →
`openbb-data` (HTTP, internal network) → free upstreams. Same sidecar pattern as
Fava and the Wealthfolio exporter; no provider key ever reaches the frontend.

## Integration shape — OpenBB Path B

`openbb-data` runs OpenBB Platform's REST API (`uvicorn
openbb_core.api.rest_api:app`) as an **isolated compose service** behind the
`macro` profile (`docker compose --profile macro up -d`). Internal port `6900`,
**no host exposure**. Heavy + opt-in: the default stack runs without it, and
`MacroAdapter` degrades cleanly (502 / stale) when it's down.

**Network isolation:** `openbb-data` sits on a dedicated `macro-net` (not the
default app network); only `fastapi-backend` is attached to both. The
`react-frontend` container therefore cannot reach it at all (verified). `macro-net`
is *not* `internal` — the sidecar still needs egress to the free upstream
providers. The REST API has no auth (fine — it's unreachable except by the backend).

Image: `openbb-data/Dockerfile` — `python:3.12-slim` + only the needed
extensions (~593 MB):

- routers: `openbb-economy`, `openbb-fixedincome`
- providers (free): `openbb-federal-reserve`, `openbb-ecb`, `openbb-econdb`,
  `openbb-fred`, `openbb-yfinance`

A build-time smoke check (`from openbb_core.api.rest_api import app`) fails the
build if the extension set is broken, rather than the first request.

## OpenBB envelope → Catallax `Envelope`

OpenBB returns `OBBject`: `{results: [...], provider, warnings, ...}`. The
adapter takes `results`, normalises it, and wraps it in the F1 `Envelope[T]`
(`{data, source, fetched_at, cache_hit, stale, warnings}`) so widgets render
provenance/staleness dots (`widgets/Provenance.tsx`). **Macro endpoints return
the Envelope as the body** (unlike the history route, which used `X-Catallax-*`
headers for back-compat).

## Endpoint → upstream map (verified)

| Catallax route | OpenBB call | Provider | Key? |
|---|---|---|---|
| `GET /macro/yield-curve?region=US` | `/fixedincome/government/yield_curve` | `federal_reserve` | no |
| `GET /macro/yield-curve?region=EU` | `/fixedincome/government/yield_curve` | `ecb` | no |
| `GET /macro/rates` | `/fixedincome/government/treasury_rates` | `federal_reserve` | no |
| `GET /macro/indicators` | `/economy/indicators` (per root) | `econdb` | no¹ |
| `GET /macro/series/{ROOT.country}` | `/economy/indicators` | `econdb` | no¹ |
| `GET /macro/series/{FRED_ID}` | `/economy/fred_series` | `fred` | **yes** |
| `GET /macro/calendar` | **direct FRED `releases/dates`** (not OpenBB)² | `fred` | **yes** |

² **Calendar bypasses openbb-data (2026-07-04):** OpenBB's `fred` calendar
provider fans out one request per release and reliably times out (`FRED request
failed (TimeoutError)` — the widget sat silently empty for weeks). The adapter
now calls `https://api.stlouisfed.org/fred/releases/dates`
(`fred_releases_dates_url`) directly — one request, all upcoming release dates,
names/dates only (FRED publishes no consensus/actual there). Failures surface as
an Envelope warning ("source unavailable"), never a silent `[]`; httpx exception
text is api_key-masked before logging.

Rates/curves arrive as **fractions** (0.0438) → adapter `_as_pct` → percent
(4.38). econdb indicator values are **fractions** too (0.0198 = 1.98%); the UI
multiplies by 100.

### FRED key (free, optional)

`OPENBB_FRED_API_KEY` (https://fredaccount.stlouisfed.org/apikeys). Unset =
keyless providers only (curves + indicators still work). The key flows to the
`openbb-data` container (its `entrypoint.sh` writes it into OpenBB's
`/root/.openbb_platform/user_settings.json`) **and** to the one direct FRED call
the backend makes itself — the release calendar (see table note²). Setting it
lights up FRED series, CPI, and the economic-release **calendar**. When unset,
`/macro/calendar` returns `{events: [], needs_key: true}` and the widget shows
an enablement note. The key never reaches the frontend in either path.

## econdb quirks (learned the hard way)

1. **`/economy/indicators` needs `symbol=ROOT` + `country=<comma list>`** — not
   the full `CPIUS` symbol, and **a single country 204s**; always pass the
   multi-country list (`MACRO_INDICATOR_COUNTRIES`). For a single-country series,
   `MacroAdapter.series()` requests the full list and filters (case-insensitive
   on the spaced country name, e.g. `euro_area` → "Euro area").
2. **`transform=toya`** = year-over-year; omit for raw level (e.g. unemployment).
   Config: `MACRO_INDICATOR_SET=CPI:toya,CORE:toya,PPI:toya,URATE:level,RGDP:toya`.
3. ¹**Unauthenticated econdb rate-limits by IP** — under a burst its data API
   returns **204 No Content** for every call (its static `available_indicators`
   still works). Normal use is fine (the 6 h `MACRO_CACHE_TTL_SECONDS` means ~5
   calls per 6 h), but bursty testing trips it. Mitigation below.

## Caching + stale fallback

`_cached(key, model, builder, is_empty)` → `(model, cache_hit, stale)`:

- Hit Redis (`MACRO_CACHE_TTL_SECONDS`, default 6 h — macro moves daily at most).
- On miss, build from upstream. **Empty results are never cached as good**
  (so a transient upstream blank can't poison the 6 h TTL).
- A good result is also written to a 7-day `:stale` key. When a fresh build comes
  back empty (econdb 204, FRED outage…), the **last good result** is served with
  `stale=true` + a warning → the widget shows a red provenance dot, not a blank.
  (Same pattern as the markets-crypto stale cache.)

## Adding a macro endpoint / widget

1. Add a method to `MacroAdapter` returning `Envelope[YourModel]` via `_cached`.
2. Add the route to `routers/macro.py` (allowlist any user-influenced path param).
3. Add the client method (`api.macroX`) + types (`Envelope<…>`) and a widget
   under `Macro` / `Rates` category. Register (lazy if it pulls a chart lib).
4. Update this file.

## Frontend

Widgets (`category: "Macro" | "Rates"`): `MacroHeatmap`, `RecessionSignals`,
`EconCalendar`, `YieldCurve` (eager, SVG/table); `MacroSeries`, `RateSpreads`
(lazy — lightweight-charts code-split). Tabs **Macro** + **Rates** seed the
default `Daily` workspace (`workspaces.ts` v2); palette codes `MACRO` / `RATES`.
Series ids: `ROOT.country` (keyless econdb) or a bare FRED id (needs key).
