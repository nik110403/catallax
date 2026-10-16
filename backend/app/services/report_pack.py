"""Weekly Report Pack — the chapter sweep engine.

`ReportPackEngine.build_chapters()` assembles chapters 2 (portfolio), 3
(market), 4 (holdings), 5 (quant) and 7 (methodology) purely from EXISTING
engines — `RiskEngine`, `MarketResearchEngine`, `ResearchEngine`,
`SignalEngine`, `ScannerEngine`, `IntelService` reads, `MacroAdapter`, plus
the advisor-week rows (`portfolio_snapshots`) and the frozen
`services/scorecard.compute_scorecard()`. Chapters 1 (exec summary) and 6
(shadow) are assembled elsewhere — not this module's job.

Design mirrors `services/advisor_report.py`: every engine is referenced by
its MODULE-LEVEL name (`RiskEngine`, `MarketResearchEngine`, ...) so tests can
monkeypatch the class in place, the same convention `test_advisor_report.py`
uses for `MarketResearchEngine`/`make_wealthfolio`.

Each `chapter_*()` does the real work and lets exceptions propagate; only
`build_chapters()` catches, per chapter — "each individually try/excepted"
means one dead upstream engine turns exactly ONE chapter absent
(`{"absent": True, "reason": str(exc)}`) without sinking the other four.

Forensic score deltas (Piotroski/Altman/Beneish, per the methodology 
review): these inputs are annual filings, so a naive week-over-week delta is
structurally zero ~51 weeks/year and the one nonzero week is a filing-vintage
artifact, not real deterioration. `_forensic_deltas()` instead walks this
account's own PRIOR stored `report_packs.sections` rows (already-generated
weekly packs are the only history of these scores this codebase keeps) for
the most recent one where the score actually differed, and labels the delta
against that reading's fiscal year — never "WoW".

`sources[key]` also projects markers from an embedded, externally-produced
chapter payload (currently only chapter_quant's `factor`, populated by the
quant worker or its stale-fallback — see `_EMBEDDED_PAYLOAD_KEYS` and
`_project_embedded_payload_markers()`): `symbol_resolution_failures` lands
under an additive `sources[key]["partial"]` (never dropped silently), and a
genuinely stale (cached prior-pack) factor payload sets
`sources[key]["stale"] = True` directly — that IS the codebase-wide
cached/old-data meaning of `stale`, unlike the per-symbol tear-sheet
`partial` question in `chapter_holdings`.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from app.adapters.macro_adapter import MacroAdapter
from app.config import Settings
from app.db import session_scope
from app.models import PortfolioSnapshot, ReportPack
from app.services.intel_service import IntelService
from app.services.market_research import MarketResearchEngine
from app.services.research import ResearchEngine
from app.services.risk import RiskEngine
from app.services.scorecard import HISTORY_MAX_WEEKS, compute_scorecard
from app.services.signals import ScannerEngine, SignalEngine
from app.services.wealthfolio import make_wealthfolio

logger = logging.getLogger("catallax.report_pack")

# DEBT: these three are module constants, not `REPORT_PACK_*` Settings fields —
# no chapter-tuning knobs exist in Settings; these stay local to
# this module. Revive trigger: a later task needs these configurable per
# deployment. TODO: promote to `Settings.report_pack_*`.
_NEWS_LIMIT = 80
_FORENSIC_HISTORY_WEEKS = 12
_ECON_CALENDAR_DAYS_AHEAD = 7

_METHODOLOGY_VERSION = "phase17-s2-v2"
_METHODOLOGY_APPENDIX = [
    {
        "tool": "risk.RiskEngine",
        "measures": "Historical-simulation VaR/CVaR, factor exposure (beta/vol/drawdown), "
                    "risk contributors, FX drag, concentration, stress scenarios.",
        "endpoint": "/api/v1/risk/*",
        "limitations": "Daily-close history only; static asset-class proxy shocks for "
                        "stress testing, not a per-asset historical replay.",
        "grading_horizon": "n/a — descriptive risk metrics, not a predictive signal",
    },
    {
        "tool": "market_research.MarketResearchEngine",
        "measures": "Regime composite, cross-asset/sector/factor rotation + RRG, "
                    "volatility, breadth, sentiment (Fear & Greed + COT), CAPE/ERP.",
        "endpoint": "/api/v1/market-research/*",
        "limitations": "Regime weights and lookback windows are configured constants; "
                        "breadth oscillators need ~40 persisted sessions to warm up.",
        "grading_horizon": "n/a — descriptive market context",
    },
    {
        "tool": "research.ResearchEngine (forensic scores)",
        "measures": "Piotroski F-Score, Altman Z-Score, Beneish M-Score — textbook "
                     "formulae over yfinance annual statements.",
        "endpoint": "/api/v1/research/{symbol}/scores",
        "limitations": "Inputs are annual filings — scores are stable for ~51 weeks/year "
                        "and only move when a new filing lands; a missing line item "
                        "reports the component `passed=None`, never a silent zero.",
        "grading_horizon": "one fiscal year (annual filing cadence)",
    },
    {
        "tool": "signals.SignalEngine",
        "measures": "Signal IC/ICIR, quantile spreads, hit rate, cross-sectional ranks "
                     "over the configured factor universe.",
        "endpoint": "/api/v1/signals/*",
        "limitations": "Cross-section ranks sliced to currently-held symbols are a "
                        "within-book ordering, not an independent factor reading, once "
                        "the universe is as small as a typical holdings book.",
        "grading_horizon": "configurable, default `signal_default_horizon_bars`",
    },
    {
        "tool": "signals.ScannerEngine",
        "measures": "Chart-pattern detection (technical scan) over daily OHLCV.",
        "endpoint": "/api/v1/scan",
        "limitations": "Pattern detectors are heuristic; a hit is a technical observation, "
                        "not a trade recommendation.",
        "grading_horizon": "n/a — point-in-time detection, not graded",
    },
    {
        "tool": "intel_service.IntelService",
        "measures": "RSS-sourced news enrichment (sentiment, topics, clustering).",
        "endpoint": "/api/v1/ai/news",
        "limitations": "Sentiment/summary are model inference (grounding=speculative); "
                        "holdings filtering here is a case-insensitive substring match "
                        "on title/summary/topics, not an entity-linked feed.",
        "grading_horizon": "n/a",
    },
    {
        # the methodology appendix used to omit the
        # shadow-grading engine entirely (chapter 6, assembled by the report worker from
        # the `shadow_verdicts` table) — this appendix must cover every
        # analytical tool the pack surfaces, not just this module's own five.
        "tool": "shadow_grading (ShadowVerdict / S3)",
        "measures": "Deterministic per-tool verdicts (add_watch / hold / trim_watch / flag) "
                    "graded against what actually happened afterward — evidence-only, never "
                    "a claim of predictive skill.",
        "endpoint": "n/a — chapter 6 of the weekly report pack (assembled by S3 from the "
                    "`shadow_verdicts` table)",
        "benchmark": "Cross-sectional tools (e.g. quant ranks) are benchmarked against the "
                     "full-cross-section book-median excess return; absolute-call tools "
                     "(e.g. forensic flags) are benchmarked against the event-sample up-rate "
                     "(how often that verdict type was directionally right across all graded "
                     "events, not just this one).",
        "limitations": "Small N (weekly cadence over a single book means few graded events "
                       "accumulate); an overlap veto suppresses a verdict's grading when "
                       "multiple tools fire on the same symbol/week (can't attribute the "
                       "outcome to one); forensic-score verdicts are NOT gradeable at all "
                       "(annual-filing cadence has no 4-week forward window to grade against).",
        "grading_horizon": "4-week forward window from the verdict's report_week",
    },
]

_ENGINE_NAMES = {
    "portfolio": "risk.RiskEngine + services.scorecard.compute_scorecard",
    "market": "market_research.MarketResearchEngine + adapters.macro_adapter.MacroAdapter",
    "holdings": "research.ResearchEngine + signals.ScannerEngine + intel_service.IntelService",
    "quant": "signals.SignalEngine",
    "methodology": "static",
}


def _snapshot_dict(row: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "report_week": row.report_week, "symbol": row.symbol,
        "qty": float(row.qty), "value_eur": float(row.value_eur),
        "nav": float(row.nav), "cash": float(row.cash),
    }


def _filter_news(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """Matches against the FULL `IntelService.news()` row (title/summary/
    topics) — `summary` is used for matching only and never stored (see
    `_project_news_item` SEV3)."""
    needle = symbol.upper()
    out = []
    for r in rows:
        haystack = f"{r.get('title') or ''} {r.get('summary') or ''}".upper()
        topics = [str(t).upper() for t in (r.get("topics") or [])]
        if needle in haystack or needle in topics:
            out.append(r)
    return out


def _project_news_item(row: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted projection of a matched news row for STORAGE into
    `sections`: the raw `IntelService.news()` row
    also carries `summary`/`grounding`/`cluster_id`/`country`/`lat`/`lon` —
    none of that belongs in a persisted, potentially-narrated report pack.
    Named fields only, nothing else survives."""
    return {
        "title": row.get("title"), "source": row.get("source"),
        "published": row.get("published"), "sentiment": row.get("sentiment"),
        "score": row.get("sentiment_score"), "topics": row.get("topics"),
        "link": row.get("link"),
    }


# Which chapter key embeds an externally-produced payload (i.e. not built by
# `ReportPackEngine` itself) that may carry `symbol_resolution_failures`/
# `stale` markers needing sources-level projection. Currently only
# chapter_quant's `factor` (the in-process factor study, or the
# stale-fallback of the last completed pack's reading) does this — add a
# `{chapter_key: embedded_field_name}` entry here for any future chapter
# that embeds a payload following the same convention; `build_chapters`'
# projection loop picks it up with no further change.
_EMBEDDED_PAYLOAD_KEYS = {"quant": "factor"}


def _project_embedded_payload_markers(entry: dict[str, Any], payload: Any) -> None:
    """Mutate a `sources[key]` `entry` in place with markers found on an
    embedded, externally-produced chapter payload (see
    `_EMBEDDED_PAYLOAD_KEYS`) — so a consumer scanning `sources` alone sees
    per-symbol resolution failures and genuine staleness without opening the
    chapter body.

    `stale` here IS genuine cached/old-data staleness (the worker's stale-fallback
    ships the latest COMPLETED prior pack's factor reading when this week's
    quant work times out/fails) — the codebase-wide `stale` meaning applies
    correctly, unlike the per-symbol tear-sheet `partial` question resolved
    earlier. `symbol_resolution_failures` (some held
    symbols couldn't be resolved to a price series) is a distinct, additive
    condition and always lands under `partial`, never folded into `stale`.
    """
    if not isinstance(payload, dict):
        return
    failures = payload.get("symbol_resolution_failures")
    if failures:
        partial = entry.setdefault("partial", {})
        partial["symbol_resolution_failures"] = failures
        partial["n_symbols_resolution_failed"] = payload.get(
            "n_symbols_resolution_failed", len(failures)
        )
    if payload.get("stale"):
        entry["stale"] = True


class ReportPackEngine:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── ch2: portfolio review ───────────────────────────────────
    async def chapter_portfolio(self, report_week: str) -> dict[str, Any]:
        async with session_scope() as db:
            weeks = (await db.execute(
                select(PortfolioSnapshot.report_week).distinct()
                .where(PortfolioSnapshot.report_week <= report_week)
                .order_by(PortfolioSnapshot.report_week.desc())
                .limit(HISTORY_MAX_WEEKS)
            )).scalars().all()
            history_rows = []
            if weeks:
                history_rows = (await db.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.report_week.in_(weeks))
                    .order_by(PortfolioSnapshot.report_week, PortfolioSnapshot.symbol)
                )).scalars().all()

        history = [_snapshot_dict(r) for r in history_rows]
        current = [s for s in history if s["report_week"] == report_week]
        prior_week = weeks[1] if len(weeks) > 1 else None
        prior_by_symbol = {s["symbol"]: s for s in history if prior_week and s["report_week"] == prior_week}
        prior_nav = next((s["nav"] for s in history if prior_week and s["report_week"] == prior_week), None)
        nav = current[0]["nav"] if current else 0.0
        broker_cash = current[0]["cash"] if current else 0.0

        # Cash-equivalent holdings (stablecoins) are treated as CASH, mirroring
        # the frozen advisor (advisor.py: excluded from its weight vector, their
        # value simply isn't in it) — they never get a position/weight row here
        # either, their value is folded into `cash_eur` instead.
        ce_symbols = self._settings.advisor_cash_equivalent_symbols_set
        positions = []
        cash_equivalents = []
        cash_equivalents_eur = 0.0
        for s in current:
            if s["symbol"] in ce_symbols:
                cash_equivalents.append(s["symbol"])
                cash_equivalents_eur += s["value_eur"]
                continue
            weight_pct = round(s["value_eur"] / nav * 100, 4) if nav else 0.0
            p = prior_by_symbol.get(s["symbol"])
            wow_value_delta_eur = round(s["value_eur"] - p["value_eur"], 2) if p else None
            wow_weight_delta_pct = None
            if p and prior_nav:
                wow_weight_delta_pct = round(weight_pct - (p["value_eur"] / prior_nav * 100), 4)
            positions.append({
                "symbol": s["symbol"], "qty": s["qty"], "value_eur": s["value_eur"],
                "weight_pct": weight_pct, "wow_value_delta_eur": wow_value_delta_eur,
                "wow_weight_delta_pct": wow_weight_delta_pct,
            })
        cash_equivalents_eur = round(cash_equivalents_eur, 2)
        cash = round(broker_cash + cash_equivalents_eur, 2)

        bundle = await RiskEngine(self._redis, self._settings).bundle()
        performance = compute_scorecard(history)

        return {
            "engine": _ENGINE_NAMES["portfolio"],
            "how_to_read": (
                "Positions/weights as of this week's portfolio_snapshots row; WoW deltas "
                "compare against the prior ISO week's snapshot (None for a symbol with no "
                "prior-week row — a new position, not a zero change). VaR/CVaR + coverage "
                "come straight from the live risk bundle; the scorecard is the same "
                "two-arm (actual vs do-nothing) TWR comparison the Advisor weekly report "
                "uses, passed through untouched. `cash_eur` = broker cash + cash-equivalent "
                "holdings (stablecoins, `cash_equivalents_eur`/`cash_equivalents` below) — "
                "the latter never appear as a position/weight row, mirroring the advisor."
            ),
            "report_week": report_week, "prior_report_week": prior_week,
            "nav_eur": nav, "cash_eur": cash,
            "cash_equivalents_eur": cash_equivalents_eur, "cash_equivalents": cash_equivalents,
            "positions": positions,
            "var_cvar": bundle.get("var"),
            "coverage": bundle.get("coverage"),
            "scorecard": performance,
        }

    # ── ch3: market context ─────────────────────────────────────
    async def chapter_market(self) -> dict[str, Any]:
        bundle = await MarketResearchEngine(self._redis, self._settings).bundle()
        calendar_env = await MacroAdapter(self._redis, self._settings).calendar(
            days=_ECON_CALENDAR_DAYS_AHEAD
        )
        return {
            "engine": _ENGINE_NAMES["market"],
            "how_to_read": (
                "Regime composite + its weighted components; cross-asset correlations; "
                "sector/factor rotation and its RRG view; realized/implied volatility; "
                "market breadth oscillators; sentiment (CNN Fear & Greed + CFTC "
                "Commitments of Traders); CAPE/ERP valuation; and the week-ahead economic "
                "release calendar. All descriptive market context, not a forecast."
            ),
            "regime": bundle.get("regime"),
            "cross_asset": bundle.get("cross_asset"),
            "rotation": bundle.get("rotation"),
            "rrg": bundle.get("rrg"),
            "volatility": bundle.get("volatility"),
            "breadth": bundle.get("breadth"),
            "sentiment": bundle.get("sentiment"),
            "valuation": bundle.get("valuation"),
            "econ_calendar_week_ahead": calendar_env.model_dump(mode="json"),
        }

    # ── forensic score deltas (filing-vintage, never WoW) ───────
    async def _forensic_deltas(
        self, symbol: str, current_cards: list[dict[str, Any]], current_fiscal_years: list[str]
    ) -> dict[str, dict[str, Any]]:
        async with session_scope() as db:
            rows = (await db.execute(
                select(ReportPack.report_week, ReportPack.sections)
                .where(ReportPack.status == "complete")
                .order_by(ReportPack.report_week.desc())
                .limit(_FORENSIC_HISTORY_WEEKS)
            )).all()

        current_by_key = {c["key"]: c.get("score") for c in current_cards}
        deltas: dict[str, dict[str, Any]] = {}
        for key, cur_score in current_by_key.items():
            deltas[key] = {
                "delta": None, "vs_report_week": None, "vs_fiscal_year": None,
                "label": "no prior stored reading to compare against",
            }
            if cur_score is None:
                continue
            # Distinguish "no prior reading at all"
            # from "prior readings exist but are all identical" — the latter
            # is the NORMAL steady state (~51 weeks/year, no new filing) and
            # must never be mislabeled as "no prior stored reading", which is
            # false the moment a single prior week's reading exists.
            # `last_seen` is the most recent PRIOR reading found for this
            # key, regardless of whether it ties with `cur_score`.
            last_seen: tuple[str, str | None] | None = None
            for prior_week, sections in rows:
                symbols = (((sections or {}).get("holdings") or {}).get("symbols")) or {}
                forensic = (symbols.get(symbol) or {}).get("forensic") or {}
                prior_cards = {c["key"]: c.get("score") for c in (forensic.get("cards") or [])}
                prior_score = prior_cards.get(key)
                if prior_score is None:
                    continue
                prior_fy = (forensic.get("fiscal_years") or [None])[0]
                if last_seen is None:
                    last_seen = (prior_week, prior_fy)
                if not math.isclose(prior_score, cur_score, rel_tol=1e-9, abs_tol=1e-9):
                    deltas[key] = {
                        "delta": round(cur_score - prior_score, 4),
                        "vs_report_week": prior_week, "vs_fiscal_year": prior_fy,
                        "label": (
                            f"filing-vintage delta vs report {prior_week} (FY {prior_fy}) — "
                            "NOT a week-over-week change; these scores only move on a new "
                            "annual filing"
                        ),
                    }
                    break
            else:
                if last_seen is not None:
                    seen_week, seen_fy = last_seen
                    deltas[key] = {
                        "delta": 0.0, "vs_report_week": seen_week, "vs_fiscal_year": seen_fy,
                        "label": (
                            f"unchanged since report {seen_week} (FY {seen_fy}) — no new "
                            "filing yet across the stored history checked"
                        ),
                    }
        return deltas

    # ── ch4: holdings deep-dive ──────────────────────────────────
    async def chapter_holdings(self) -> dict[str, Any]:
        holdings = await make_wealthfolio(self._redis, self._settings).holdings()
        # Cash-equivalent holdings (stablecoins) are treated as CASH (mirrors
        # advisor.py) — no tear sheet is built for them; they're recorded
        # separately so coverage stays honest (not silently dropped).
        ce_symbols = self._settings.advisor_cash_equivalent_symbols_set
        all_symbols = sorted({a.symbol.upper() for a in holdings.by_asset})
        # The holding's KNOWN Wealthfolio class decides the fundamentals crypto
        # skip — never the ticker spelling (review 225d5f4: an equity ticked
        # `SOL` must still get statements). Blank/missing -> heuristic fallback.
        class_by_symbol = {
            a.symbol.upper(): (getattr(a, "asset_class", None) or "").upper() or None
            for a in holdings.by_asset
        }
        cash_equivalents_excluded = [s for s in all_symbols if s in ce_symbols]
        symbols = [s for s in all_symbols if s not in ce_symbols]
        research = ResearchEngine(self._redis, self._settings)
        scanner = ScannerEngine(self._redis, self._settings)
        news_rows = await IntelService(self._redis, self._settings).news(limit=_NEWS_LIMIT)

        per_symbol: dict[str, Any] = {}
        symbols_failed: dict[str, str] = {}
        # Sequential across symbols — never asyncio.gather here: each symbol
        # already fans out several rate-limited adapter calls (fundamentals,
        # valuation, technicals, scan), and doing that N-symbols-wide at once
        # would blow through the adapters' own rate limiting.
        #
        # Per-symbol try/except: "a dead source
        # never sinks the pack" applies WITHIN this chapter too — a delisted
        # ticker or a missing-filing symbol must not blank every OTHER held
        # symbol's tear sheet. Failures are isolated to that symbol's slot
        # and aggregated below so build_chapters' `sources["holdings"]` can
        # surface them; only holdings ENUMERATION itself (make_wealthfolio
        # above) still fails the whole chapter, same as before.
        for symbol in symbols:
            try:
                asset_class = class_by_symbol.get(symbol)
                fund, _ = await research.fundamentals(symbol, asset_class=asset_class)
                valuation, _ = await research.valuation(symbol, asset_class=asset_class)
                scores, _ = await research.scores(symbol, asset_class=asset_class)
                tech, _ = await research.technicals(symbol, asset_class=asset_class)
                scan = await scanner.scan(patterns=None, universe=[symbol])

                forensic = scores.model_dump()
                forensic["deltas"] = await self._forensic_deltas(
                    symbol, forensic["cards"], forensic["fiscal_years"]
                )
                per_symbol[symbol] = {
                    "fundamentals": fund.model_dump(),
                    "valuation": valuation.model_dump(),
                    "forensic": forensic,
                    "technicals": tech.model_dump(),
                    "scan": scan.model_dump(),
                    "news": [_project_news_item(r) for r in _filter_news(news_rows, symbol)],
                }
            except Exception as exc:
                logger.warning("report_pack holdings tear sheet failed for %s: %s", symbol, exc)
                per_symbol[symbol] = {"absent": True, "reason": str(exc)}
                symbols_failed[symbol] = str(exc)

        return {
            "engine": _ENGINE_NAMES["holdings"],
            "how_to_read": (
                "Per-symbol deep dive over currently held positions only: fundamentals, "
                "valuation, the three forensic scores, technicals, chart-pattern scan "
                "hits, and holdings-filtered intel news. Forensic score deltas compare "
                "against the last STORED reading where the score actually changed "
                "(annual-filing cadence) — never a week-over-week figure, since these "
                "inputs move only on a new filing. A symbol whose tear sheet couldn't be "
                "built (delisted ticker, missing filing data, upstream outage) is marked "
                "`absent` in its own slot only — every other held symbol is unaffected. "
                "Cash-equivalent holdings (stablecoins) are treated as cash, not a position "
                "— never get a tear sheet, listed instead in `cash_equivalents_excluded`."
            ),
            "symbols": per_symbol,
            "symbols_failed": symbols_failed,
            "n_symbols_failed": len(symbols_failed),
            "cash_equivalents_excluded": cash_equivalents_excluded,
        }

    # ── ch5: quant ────────────────────────────────────────────────
    async def chapter_quant(self, factor: dict[str, Any] | None) -> dict[str, Any]:
        signals = SignalEngine(self._redis, self._settings)
        holdings = await make_wealthfolio(self._redis, self._settings).holdings()
        # Cash-equivalent holdings (stablecoins) are treated as CASH (mirrors
        # advisor.py) — excluded from the held-set so they never get a
        # cross-sectional signal reading.
        ce_symbols = self._settings.advisor_cash_equivalent_symbols_set
        held = {a.symbol.upper() for a in holdings.by_asset if a.symbol.upper() not in ce_symbols}

        scoreboard = await signals.scoreboard()
        catalog = await signals.catalog()
        cross_sections: dict[str, Any] = {}
        for entry in catalog.signals:
            if not entry.last_date:
                continue
            cs = await signals.cross_section(entry.signal_id, entry.last_date)
            held_rows = [r.model_dump() for r in cs.rows if r.symbol.upper() in held]
            if held_rows:
                cross_sections[entry.signal_id] = {
                    "as_of_date": cs.as_of_date, "n_universe": len(cs.rows), "rows": held_rows,
                }

        return {
            "engine": _ENGINE_NAMES["quant"],
            "how_to_read": (
                "The in-process factor study (IC, quantiles) is embedded below when the "
                "worker has run one this week; absent otherwise. Signal scoreboard grades "
                "are computed live over each catalog signal's full configured universe. "
                "The cross-section ranks are sliced to currently-held symbols only — with "
                "a typical holdings book (~10 names), that slice is a within-book "
                "ordering, not an independent market factor reading."
            ),
            "factor": factor if factor is not None else {
                "absent": True, "reason": "no factor study payload for this report week",
            },
            "scoreboard": scoreboard.model_dump(),
            "cross_section": cross_sections,
        }

    # ── ch7: methodology appendix ───────────────────────────────
    async def chapter_methodology(self) -> dict[str, Any]:
        return {
            "engine": _ENGINE_NAMES["methodology"],
            "how_to_read": (
                "Reference appendix: what each analytical tool in this pack measures, "
                "where it reads from, its known limitations, and its grading horizon. "
                "Static content, versioned below — not recomputed per report."
            ),
            "version": _METHODOLOGY_VERSION,
            "tools": _METHODOLOGY_APPENDIX,
        }

    # ── sweep: chapters 2/3/4/5/7 ────────────────────────────────
    async def build_chapters(
        self, report_week: str, factor: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        calls = {
            "portfolio": lambda: self.chapter_portfolio(report_week),
            "market": lambda: self.chapter_market(),
            "holdings": lambda: self.chapter_holdings(),
            "quant": lambda: self.chapter_quant(factor),
            "methodology": lambda: self.chapter_methodology(),
        }
        sections: dict[str, Any] = {}
        sources: dict[str, Any] = {}
        for key, call in calls.items():
            fetched_at = datetime.now(timezone.utc).isoformat()
            try:
                section = await call()
                sections[key] = section
                entry = {
                    "engine": _ENGINE_NAMES[key], "fetched_at": fetched_at,
                    "stale": False, "error": None,
                }
                # Chapter-level success can still carry PARTIAL degradation
                # (e.g. holdings' per-symbol failures)
                # — surface it on the source entry too, not just buried in
                # the chapter body, so a consumer scanning `sources` alone
                # still sees the pack is degraded. `stale` keeps its
                # codebase-wide meaning (old/cached data, see research.py /
                # market_research.py / signals.py) — a missing per-symbol
                # tear sheet is a distinct condition, so it's an ADDITIVE
                # `partial` key, never folded into `stale`.
                symbols_failed = section.get("symbols_failed") if isinstance(section, dict) else None
                if symbols_failed:
                    entry["partial"] = {
                        "symbols_failed": symbols_failed, "n_symbols_failed": len(symbols_failed),
                    }
                # Generic hook (see `_EMBEDDED_PAYLOAD_KEYS`): project an
                # embedded externally-produced payload's own markers (the
                # factor study / the stale-fallback) onto this same entry.
                embedded_key = _EMBEDDED_PAYLOAD_KEYS.get(key)
                if embedded_key and isinstance(section, dict):
                    _project_embedded_payload_markers(entry, section.get(embedded_key))
                sources[key] = entry
            except Exception as exc:  # a dead source never sinks the pack
                logger.warning("report_pack chapter '%s' failed: %s", key, exc)
                sections[key] = {"absent": True, "reason": str(exc)}
                # `stale` means "old/cached data being served" (codebase-wide
                # convention) — a hard chapter failure serves NOTHING, so it
                # is not staleness; the `error` field alone carries the
                # reason ( : the old `stale: True` here
                # contradicted that same convention this file otherwise
                # upholds for the `partial`/embedded-payload projection).
                sources[key] = {
                    "engine": _ENGINE_NAMES[key], "fetched_at": fetched_at,
                    "stale": False, "error": str(exc),
                }
        return sections, sources
