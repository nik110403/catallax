"""Shadow verdict engine — deterministic, evidence-only shadow
grading of every advisory "tool" the report pack surfaces. NOTHING here ever
proposes a trade; it records what a tool WOULD have said and, later, whether
that call would have paid off. The review rulings below are
binding and enforced in code, not just documented.

`ShadowEngine.compute_verdicts()` is a pure deterministic mapping (same
`(report_week, readings)` -> byte-identical rows) plus an idempotent upsert on
`shadow_verdicts`' `(report_week, symbol, tool)` unique key. `readings` is
supplied by the caller (the report worker, which owns building it from the
chapter engines already in this codebase) shaped as:

    readings: dict[tool, dict[symbol, reading]]

    tool == "technicals" — reading = {"sma50": float|None, "sma200": float|None,
        "rsi_14": float|None} (the fields `research.py`'s `TechnicalReport`
        already computes from the F0 OHLCV backbone).
    tool == "patterns" — reading = {"score": int} (`research.py`'s
        `ScannerEngine`... `ScanRow.score`, the net bias sum of matched scan
        patterns; 0 or absent = no directional hit).
    tool == "forensic" — reading = {"altman_verdict": str|None,
        "beneish_verdict": str|None, "piotroski_score": float|None} (the
        `ScoreCard.verdict`/`.score` fields `research.py` already produces for
        the Altman/Beneish/Piotroski cards — verdict strings are matched
        verbatim: "Distress", "Elevated manipulation risk").
    tool in ("factor", "signals") — reading = {"value": float} (a
        cross-sectional score for the week: a factor-study exposure/rank for
        `factor`, a signal-scoreboard directional score for `signals`).
        Ranked into terciles ACROSS all symbols present for that tool this
        week — never read as an absolute-price call.

Every reading MAY also carry an optional `"source"` (F0 adapter name,
default `price_source_default`) — echoed into the persisted `reading` JSONB
and read back by `scoreboard()` when it later prices the verdict. A
`"exited"` override MAY also be supplied, but grading no longer trusts a
producer to set it (no producer does): a symbol is inferred `exited` at
grading time when it has verdict history for a tool but no row in that
tool's LATEST verdict week (it left the book) — see `ShadowEngine.scoreboard`.

── Statistical conditions ─────────────────
`scoreboard()` grades verdict-CHANGE events — `_collapse_events` opens a new
event only on a DIFFERENT non-"hold" verdict; "hold" weeks are a transparent
gap that neither opens nor closes an event, so RSI hovering at a hard
threshold (add_watch/hold/add_watch/hold/...) still collapses to ONE event,
not one per flap. Prices are fetched a fixed 4 weeks (`_GRADE_HORIZON_WEEKS`)
forward by symbol regardless of current holding status (F0 `HistoryStore`) —
that constant is DECOUPLED from `settings.report_pack_shadow_grade_min_weeks`
(the calendar-history maturity gate); conflating the two made `low_confidence`
meaningless. `factor`/`signals` are graded cross-sectionally against the
book-median excess return computed over the FULL covered cross-section for
that week (every matured name, including "hold"/mid-tercile ones — grading a
relative call against a median built from only the relative calls themselves
is circular); a zero-excess (or exactly-flat absolute return) observation is
a PUSH, excluded from both hits and misses and counted separately. Because a
tercile pick pairs one add_watch against one trim_watch per week from the
same underlying ranking, cross-sectional `n_graded` double-counts
independent comparisons — `effective_n = n_graded // 2` and the Wilson lower
bound for cross-sectional tools is computed on `effective_n`, not `n_graded`
(absolute-direction tools keep `n_graded`). The overlap veto also fires when
every graded observation shares one calendar week (not just when windows
literally overlap), and a small-sample warning fires whenever the graded (or
effective) N is below 5, independent of `low_confidence` — the two used to
never coexist by construction, making `low_confidence` alone vacuous.
`priced_as_of` timestamps when the forward returns were fetched. Uncertainty
throughout is Wilson-lower-bound (reimplemented below — NOT imported from
`signals.py`, to avoid coupling; NOT `scorecard.py`'s sign-test, wrong
instrument at this N); every payload embeds
`"nature": "descriptive, evidence-only, never a proposal"`. Forensic
verdicts NEVER enter the graded denominator — their annual/quarterly horizon
exceeds the 4-week window; `direction` is NULL for every forensic row
independent of verdict.

── Calibration ──────────────────────────────────────────────
`ShadowEngine.calibration()`/`chapter_calibration()` grade the FORECAST
LEDGER (`forecasts` ⋈ `outcomes`), not `shadow_verdicts` directly —
a separate, complementary read of the same underlying calls, one row per
issued forecast rather than per verdict-change event. This absorbs Phase
16's deferred "v1.5 grader" (a probabilistic, Brier-scored companion to the
binary hit-rate scoreboard above), finally wired up now that the ledger
gives every issuer's calls a frozen, point-in-time probability to score.
Grouped by `issued_by` × `horizon_days`. Machine (`shadow:<tool>`) issuers
carry a near-constant per-week probability (their own Laplace track-record
prior) — a reliability-bin histogram over that is tautological, so those
groups show discrimination stats (hit_rate vs benchmark_hit_rate) and a
climatological Brier reference instead (B2); only varying-probability
(`human`) issuers get bins. Cross-sectional issuers (`shadow:factor`,
`shadow:signals`) count observations at ½ weight (`effective_n = n // 2`),
mirroring `_score_tool`'s own B4 rule. `brier_ref = p̄(1-p̄)` where `p̄` is
the group's own realised hit base rate (the climatological forecaster) —
never a flat 0.25 coin-flip, which is kept only as a labelled second column
(`brier_coin`) for reference, never as the skill denominator.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.adapters.factory import build_adapter
from app.config import Settings
from app.db import session_scope
from app.errors import CatallaxError, RateLimitExceeded, SymbolNotFound
from app.models import Forecast, Outcome, ShadowVerdict
from app.services.history import HistoryStore

logger = logging.getLogger("catallax.shadow")

_Z95 = 1.959963984540054  # normal 95% two-sided z (Wilson lower bound)
_GRADE_HORIZON_WEEKS = 4  # the 4-week forward window prices are graded over —
# fixed and deliberately DECOUPLED from settings.report_pack_shadow_grade_min_weeks
# (the calendar-history maturity gate); conflating the two made low_confidence
# meaningless.
_SMALL_SAMPLE_N = 5  # below this (graded, or effective for cross-sectional
# tools) N, warn regardless of low_confidence — a long calendar history can
# still collapse to a tiny graded sample.

_RATE_LIMIT_WAIT_RETRIES = 3  # bound on how many times fetch_forward_return waits
# out a local (this-process, fixed-window) rate limit before giving up —
# batch-only callers (scoreboard grading, forecast_resolver.resolve_due; no
# interactive route calls this), so blocking here is acceptable.

NATURE = "descriptive, evidence-only, never a proposal"
TOOLS: tuple[str, ...] = ("technicals", "patterns", "forensic", "factor", "signals")
_FORENSIC_HORIZON_NOTE = (
    "horizon (quarters–2y, annual inputs) exceeds grading window"
)


def _f(x: Any, nd: int = 4) -> float | None:
    """JSON-safe float: NaN/inf/None -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _wilson_lb(hits: int, n: int, z: float = _Z95) -> float | None:
    """Reimplemented (not imported) from `signals.py`'s `_wilson_lb` — same
    5-line formula, deliberately duplicated so this module has no import edge
    onto the signals engine."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _week_start(report_week: str) -> date:
    """Monday of ISO week `"YYYY-Www"` — the verdict's decision-date anchor."""
    year_s, week_s = report_week.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1)


def _weeks_between(start_week: str, end_week: str) -> int:
    """Calendar weeks between two ISO `"YYYY-Www"` weeks (Monday-anchored,
    `start_week` <= `end_week`). Used for the calendar-time maturity gate —
    NOT a count of verdict-change events (see `config.py`'s
    `report_pack_shadow_grade_min_weeks`: "minimum WEEKS of shadow_verdicts
    history", a units-of-time gate, independent of how many times the
    verdict changed within that span)."""
    return (_week_start(end_week) - _week_start(start_week)).days // 7


# ── per-tool verdict mappers (pure; each edge threshold is unit-tested) ────
def _verdict_technicals(
    reading: dict[str, Any], overbought: float, oversold: float
) -> tuple[str, int | None, str]:
    sma50, sma200, rsi = reading.get("sma50"), reading.get("sma200"), reading.get("rsi_14")
    if sma50 is None or sma200 is None:
        return "hold", None, "insufficient SMA history"
    cross = sma50 - sma200
    if cross > 0:
        if rsi is not None and rsi >= overbought:
            return "hold", None, f"golden cross but RSI {rsi:.1f} ≥ overbought {overbought:.0f} — muted"
        return "add_watch", 1, f"golden cross (SMA50 {sma50:.2f} > SMA200 {sma200:.2f})"
    if cross < 0:
        if rsi is not None and rsi <= oversold:
            return "hold", None, f"death cross but RSI {rsi:.1f} ≤ oversold {oversold:.0f} — muted"
        return "trim_watch", -1, f"death cross (SMA50 {sma50:.2f} < SMA200 {sma200:.2f})"
    return "hold", None, "SMA50 == SMA200 (no cross)"


def _verdict_patterns(reading: dict[str, Any]) -> tuple[str, int | None, str]:
    score = reading.get("score") or 0
    if score > 0:
        return "add_watch", 1, f"net bullish pattern score {score:+d}"
    if score < 0:
        return "trim_watch", -1, f"net bearish pattern score {score:+d}"
    return "hold", None, "no directional pattern hit"


def _verdict_forensic(
    reading: dict[str, Any], piotroski_weak: float, piotroski_strong: float
) -> tuple[str, int | None, str]:
    """Direction is ALWAYS None ( forensic excluded from grading —
    its annual/quarterly inputs outlive the 4-week grading window)."""
    altman, beneish = reading.get("altman_verdict"), reading.get("beneish_verdict")
    piotroski = reading.get("piotroski_score")
    if altman == "Distress":
        return "flag", None, "forensic flag: Altman Z-Score distress zone"
    if beneish == "Elevated manipulation risk":
        return "flag", None, "forensic flag: Beneish M-Score elevated manipulation risk"
    if piotroski is not None and piotroski <= piotroski_weak:
        return "trim_watch", None, f"Piotroski F-Score {piotroski:.0f} ≤ {piotroski_weak:.0f}"
    if piotroski is not None and piotroski >= piotroski_strong:
        return "add_watch", None, f"Piotroski F-Score {piotroski:.0f} ≥ {piotroski_strong:.0f}"
    return "hold", None, "no forensic threshold breached"


def _tercile_verdicts(values: dict[str, float]) -> dict[str, tuple[str, int | None, str]]:
    """Rank symbols by cross-sectional value: bottom third -> trim_watch(-1),
    top third -> add_watch(+1), middle -> hold(NULL). Needs >=3 symbols for a
    tercile split (fewer -> all hold, insufficient breadth). Ties broken by
    symbol name so the ranking is stable regardless of dict iteration order."""
    n = len(values)
    out: dict[str, tuple[str, int | None, str]] = {}
    if n < 3:
        for sym in values:
            out[sym] = ("hold", None, f"insufficient breadth for tercile ranking (n={n} < 3)")
        return out
    ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    bottom_cut = n // 3
    top_cut = n - (n // 3)
    for i, (sym, val) in enumerate(ordered):
        rank = f"rank {i + 1}/{n}"
        if i < bottom_cut:
            out[sym] = ("trim_watch", -1, f"bottom tercile (value {val:.4f}, {rank})")
        elif i >= top_cut:
            out[sym] = ("add_watch", 1, f"top tercile (value {val:.4f}, {rank})")
        else:
            out[sym] = ("hold", None, f"middle tercile (value {val:.4f}, {rank})")
    return out


# ── verdict-change events (pure; a persistent flag re-emitted weekly is ONE
#    event, per the 's statistical condition) ───────────────────────
def _collapse_events(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """`rows` = flat shadow_verdicts rows (dicts with report_week/symbol/tool/
    verdict/direction/reading), any order. Returns {tool: [event, ...]},
    events sorted by report_week, one per (symbol, tool) verdict CHANGE.

    An event closes ONLY when a DIFFERENT non-"hold" verdict intervenes.
    "hold" is a transparent gap — it neither opens its own event nor closes
    a surrounding one (`A, hold, A` is ONE event, not two openings and a
    ghost hold in between). Fix round 2 (flap inflation): without this, RSI
    hovering at a hard threshold (add_watch/hold/add_watch/...) minted a new
    event on every flap, inflating N far beyond the number of real calls."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_key[(r["tool"], r["symbol"])].append(r)
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (tool, symbol), vlist in by_key.items():
        vlist.sort(key=lambda r: r["report_week"])
        active_verdict: str | None = None
        for r in vlist:
            v = r["verdict"]
            if v == "hold" or v == active_verdict:
                continue  # transparent gap, or the same call still standing
            active_verdict = v
            events[tool].append({
                "symbol": symbol, "report_week": r["report_week"],
                "verdict": v, "direction": r["direction"],
                "reading": r.get("reading") or {},
            })
    for tool in events:
        events[tool].sort(key=lambda e: (e["report_week"], e["symbol"]))
    return events


def _deflation_note(k: int) -> str:
    return (
        f"k={k} tool×metric hit-rates are shown side by side; none of the Wilson "
        "lower bounds below are adjusted for testing multiple tools at once — read "
        "this table as descriptive evidence, not a ranked significance claim."
    )


def _score_tool(
    tool: str,
    n_events: int,
    n_ungraded_null: int,
    n_pending: int,
    n_uncovered: int,
    observations: list[dict[str, Any]],
    full_cross_section: list[dict[str, Any]],
    weeks_of_history: int,
    min_weeks: int,
    horizon_days: int,
) -> dict[str, Any]:
    """Pure arithmetic over already-priced observations (no I/O).
    `observations` = graded (direction != None) matured items, each
    {"symbol","report_week","direction","forward_return","exited"}.
    `full_cross_section` = EVERY matured item that week regardless of
    direction (only populated for cross-sectional tools) — the book-median
    base for excess-return grading; empty for absolute-direction tools.

    `factor`/`signals` are graded CROSS-SECTIONALLY: excess vs the decision
    week's median forward return over the FULL covered cross-section (grading the relative calls against a median
    built from only the relative calls themselves is circular/structurally
    biased), never against absolute price direction (a units mismatch for a
    relative call). A zero-metric observation (the median
    name itself, or an exactly-flat absolute return) is a PUSH — excluded
    from both hits and misses, counted separately, and NOT part of the
    hit-rate/benchmark denominator (`n_graded - pushes`).

    Cross-sectional tools double-count: a week's tercile pick pairs one
    add_watch against one trim_watch from the SAME underlying ranking, so
    `n_graded` counts one independent comparison twice. `effective_n =
    n_graded // 2`; the Wilson lower bound for cross-sectional tools is
    computed on `effective_n` (proportion held fixed, only the interval
    width narrows less/wider correctly for the smaller independent sample);
    absolute-direction tools keep `n_graded` for Wilson.

    `low_confidence` gates on CALENDAR TIME (`weeks_of_history`, the span
    from the tool's earliest shadow_verdicts row to the week being graded)
    per `report_pack_shadow_grade_min_weeks`'s own definition — NOT on
    `n_events` (verdict-change count) and NOT on sample size. A separate
    small-sample `warnings` entry fires whenever the graded (or effective) N
    is below `_SMALL_SAMPLE_N`, independent of `low_confidence` — a tool can
    have ample calendar history yet a tiny graded sample (e.g. one long
    event), and the two conditions must be free to disagree."""
    cross_sectional = tool in ("factor", "signals")
    if cross_sectional:
        by_week: dict[str, list[float]] = defaultdict(list)
        for o in full_cross_section:
            by_week[o["report_week"]].append(o["forward_return"])
        medians = {wk: statistics.median(rets) for wk, rets in by_week.items()}
        for o in observations:
            o["excess_return"] = o["forward_return"] - medians[o["report_week"]]
        metric_key = "excess_return"
    else:
        metric_key = "forward_return"

    n = len(observations)
    pushes = sum(1 for o in observations if o[metric_key] == 0)
    decided = [o for o in observations if o[metric_key] != 0]
    n_decided = len(decided)
    hits = sum(1 for o in decided if (o["direction"] > 0) == (o[metric_key] > 0))
    bench_hits = sum(1 for o in decided if o[metric_key] > 0)
    hit_rate = hits / n_decided if n_decided else None
    benchmark_hit_rate = bench_hits / n_decided if n_decided else None

    effective_n: int | None = None
    if cross_sectional:
        effective_n = n // 2
        wilson_lb = (
            _wilson_lb(round(hit_rate * effective_n), effective_n)
            if hit_rate is not None and effective_n
            else None
        )
    else:
        wilson_lb = _wilson_lb(hits, n_decided) if n_decided else None

    weeks_sorted = sorted({o["report_week"] for o in observations})
    overlapping = False
    overlap_reason = ""
    if len(weeks_sorted) >= 2:
        gaps = [
            (_week_start(weeks_sorted[i + 1]) - _week_start(weeks_sorted[i])).days
            for i in range(len(weeks_sorted) - 1)
        ]
        median_gap = statistics.median(gaps) if gaps else 0.0
        if median_gap and horizon_days > median_gap:
            overlapping = True
            overlap_reason = (
                "overlapping grading windows (horizon > event spacing) — hit-rate/Wilson LB "
                "indicative only, no significance claim"
            )
    elif len(weeks_sorted) == 1 and n >= 2:
        # One-week blindness: multiple graded names but ZERO calendar spread
        # between them is just as non-independent as literally overlapping
        # windows — the old gap-based check never even ran with 1 week.
        overlapping = True
        overlap_reason = (
            "all graded observations share one market week — not independent, "
            "hit-rate/Wilson LB indicative only, no significance claim"
        )

    low_confidence = weeks_of_history < min_weeks
    warnings: list[str] = []
    if overlapping:
        warnings.append(overlap_reason)
    if low_confidence:
        warnings.append(
            f"only {weeks_of_history} week(s) of shadow_verdicts history (< {min_weeks}) — indicative only"
        )
    small_sample_n = effective_n if cross_sectional else n
    if small_sample_n is not None and small_sample_n < _SMALL_SAMPLE_N:
        warnings.append(
            f"small sample (n={small_sample_n}) — hit-rate/Wilson LB indicative only, "
            "regardless of calendar history"
        )

    return {
        "n_events": n_events,
        "n_graded": n,
        "pushes": pushes,
        "effective_n": effective_n,
        "n_ungraded_null_direction": n_ungraded_null,
        "n_pending": n_pending,
        "n_uncovered": n_uncovered,
        "exited_count": sum(1 for o in observations if o.get("exited")),
        "hit_rate": _f(hit_rate),
        "benchmark_hit_rate": _f(benchmark_hit_rate),
        "hit_rate_wilson_lb": _f(wilson_lb),
        "cross_sectional": cross_sectional,
        "low_confidence": low_confidence,
        "overlapping_windows": overlapping,
        "warnings": warnings,
    }


# ── Calibration — pure helpers over the forecast ledger's
#    (`forecasts` ⋈ `outcomes`) already-priced rows (no I/O) ───────────────
_CALIBRATION_LOG_CLAMP = (0.01, 0.99)  # duplicated from `forecasts.py`'s
# `_LOG_SCORE_CLAMP` — deliberately, not imported, same no-coupling
# convention `_wilson_lb` above documents.


def _brier(p: float, hit: bool) -> float:
    """`(p - hit)^2` — duplicated 1-liner from `forecasts.brier` (no import
    edge onto that service, mirrors `_wilson_lb`'s convention above)."""
    return (p - float(hit)) ** 2


def _log_score(p: float, hit: bool) -> float:
    """`ln(p)` if `hit` else `ln(1 - p)`, `p` clamped to `[0.01, 0.99]` —
    duplicated from `forecasts.log_score`."""
    clamped = min(max(p, _CALIBRATION_LOG_CLAMP[0]), _CALIBRATION_LOG_CLAMP[1])
    return math.log(clamped) if hit else math.log(1.0 - clamped)


def _bin_index(p: float, n_bins: int) -> int:
    """Equal-width bin index over `[0, 1]`, `[lo, hi)` intervals except the
    LAST bin which is closed `[lo, 1.0]` (m13 — `p=1.0` must land
    somewhere). `+1e-9` guards against float error putting an exact bin
    boundary (e.g. `p=0.6` at 5 bins) one bin short."""
    idx = int(p * n_bins + 1e-9)
    return min(max(idx, 0), n_bins - 1)


def _reliability_bins(observations: list[dict[str, Any]], n_bins: int) -> list[dict[str, Any]]:
    """`observations` = DECIDED (graded, non-push, non-abstained) items
    `{"p", "hit"}`. Equal-width bins over `[0, 1]`; EMPTY bins are listed
    with `n=0`, never dropped, so a sparse issuer's histogram stays honest
    about coverage rather than silently compressing."""
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(n_bins)]
    for o in observations:
        buckets[_bin_index(o["p"], n_bins)].append(o)
    width = 1.0 / n_bins
    out: list[dict[str, Any]] = []
    for i, obs in enumerate(buckets):
        lo, hi = i * width, 1.0 if i == n_bins - 1 else (i + 1) * width
        n = len(obs)
        hits = sum(1 for o in obs if o["hit"])
        mean_p = statistics.mean(o["p"] for o in obs) if n else None
        hit_rate = hits / n if n else None
        out.append({
            "bin": f"{lo:.1f}–{hi:.1f}", "lo": _f(lo), "hi": _f(hi), "n": n,
            "mean_p": _f(mean_p), "hit_rate": _f(hit_rate),
            "wilson_lb": _f(_wilson_lb(hits, n)) if n else None,
        })
    return out


def _calibration_summary(observations: list[dict[str, Any]], *, cross_sectional: bool) -> dict[str, Any]:
    """`observations` = every ledger row for one `(issued_by, horizon_days)`
    group that has an `outcomes` row (a `pending` forecast never reaches
    here — the caller counts those separately as `"pending"`), each
    `{"p", "hit", "benchmark_hit", "abstained"}`: `hit` is `None` on a PUSH
    (excluded from hits/misses/Brier, counted separately); `abstained=True`
    marks an `uncovered` resolution (pricing failure, excluded the same
    way, counted separately — `abstention_rate` carries the m12
    survivorship caveat in `how_to_read`, not here). `benchmark_hit` = the
    statement's metric > 0 regardless of direction (cf. `_score_tool`'s
    `benchmark_hit_rate`) — `None` when the underlying `realized` carried no
    priceable `value` (F2c: e.g. a sourced human forecast with nothing to
    grade a benchmark off of, distinct from `abstained`/pricing-failure).
    Benchmark stats (`benchmark_hit_rate`, `benchmark_brier`) are computed
    ONLY over the `decided` rows with `benchmark_hit is not None`
    (`benchmark_n`, exposed) — folding `None` in as a false miss silently
    dragged both down; `benchmark_n == 0` means both are `None`, never a
    fabricated `0.0`/`1.0`.

    `brier_ref = p̄(1-p̄)` where `p̄` is this group's OWN realised hit base
    rate (`hit_rate`) — the climatological forecaster, never the flat 0.25
    coin (kept alongside, labelled, never as the skill denominator).
    `benchmark_brier` is the Brier score a forecaster who always said
    `p=benchmark_hit_rate` would have earned against the SAME `benchmark_n`
    rows' realised hits. `brier_skill = 1 - brier_mean / brier_ref` (`None`
    when `brier_ref == 0` or `n == 0` — no division by zero, no skill claim
    on an empty group). `effective_n = n // 2` for cross-sectional issuers
    (B4, mirrors `_score_tool`) and Wilson uses it there instead of `n`."""
    abstained = sum(1 for o in observations if o.get("abstained"))
    pushes = sum(1 for o in observations if not o.get("abstained") and o.get("hit") is None)
    decided = [o for o in observations if not o.get("abstained") and o.get("hit") is not None]
    n = len(decided)
    effective_n = n // 2 if cross_sectional else n
    hits = sum(1 for o in decided if o["hit"])
    bench_decided = [o for o in decided if o.get("benchmark_hit") is not None]
    benchmark_n = len(bench_decided)
    bench_hits = sum(1 for o in bench_decided if o["benchmark_hit"])
    hit_rate = hits / n if n else None
    benchmark_hit_rate = bench_hits / benchmark_n if benchmark_n else None
    brier_mean = statistics.mean(_brier(o["p"], o["hit"]) for o in decided) if n else None
    log_score_mean = statistics.mean(_log_score(o["p"], o["hit"]) for o in decided) if n else None
    brier_ref = hit_rate * (1 - hit_rate) if hit_rate is not None else None
    benchmark_brier = (
        statistics.mean(_brier(benchmark_hit_rate, o["hit"]) for o in bench_decided)
        if benchmark_n and benchmark_hit_rate is not None else None
    )
    brier_skill = (
        1 - brier_mean / brier_ref if (brier_ref and brier_mean is not None) else None
    )
    if cross_sectional:
        wilson_lb = (
            _wilson_lb(round(hit_rate * effective_n), effective_n)
            if hit_rate is not None and effective_n else None
        )
    else:
        wilson_lb = _wilson_lb(hits, n) if n else None
    denom = n + pushes + abstained
    abstention_rate = abstained / denom if denom else None
    return {
        "n": n,
        "effective_n": effective_n,
        "brier_mean": _f(brier_mean),
        "brier_ref": _f(brier_ref),
        "brier_coin": 0.25,
        "benchmark_n": benchmark_n,
        "benchmark_brier": _f(benchmark_brier),
        "brier_skill": _f(brier_skill),
        "log_score_mean": _f(log_score_mean),
        "hit_rate": _f(hit_rate),
        "benchmark_hit_rate": _f(benchmark_hit_rate),
        "wilson_lb": _f(wilson_lb),
        "pushes": pushes,
        "abstained": abstained,
        "abstention_rate": _f(abstention_rate),
    }


_CALIBRATION_BINS_NOTE = (
    "issuer probability is a single track-record prior (constant per week) — reliability "
    "bins would be tautological; read discrimination (hit_rate vs benchmark_hit_rate) and "
    "brier_skill instead"
)


# ── module-level forward-return pricing (Phase 18 — lifted out of
#    `ShadowEngine._fetch_return` so the forecast resolver can share it
#    without an engine instance) ──────────────────────────────────────────
async def fetch_forward_return(
    redis: Redis, settings: Settings, symbol: str, source: str, decision: date, horizon_days: int,
    *, stale_days: int, entry_stale_days: int | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Prices a `decision -> decision + horizon_days` forward return off F0
    daily bars. Entry = first close on/after `decision`, exit = first close
    on/after `decision + horizon_days` — the exit must additionally fall
    within `stale_days` of its own anchor date (M8: the scoreboard's
    historic `+5` fetch-window tail becomes this parameter, and also bounds
    the fetch window on both sides). `entry_stale_days` is a
    SEPARATE, opt-in bound on how far the entry bar may sit after
    `decision`: `None` (the default) means NO entry check — byte-identical
    to the pre-Phase-18 `ShadowEngine._fetch_return` body, where a data gap
    right after `decision` could still match a bar weeks later as "the"
    entry. `ShadowEngine._fetch_return` (the scoreboard's grading path)
    delegates here with `stale_days=5, entry_stale_days=None` (legacy,
    Phase-17-identical); `forecast_resolver.resolve_due` calls this
    directly with both `stale_days` and `entry_stale_days` set to
    `settings.forecast_resolver_stale_days` (M8's entry bound is real there
    — a forecast ledger row must not silently mature off a stale entry).
    Returns `("pending", None)` when the horizon hasn't elapsed yet,
    `("uncovered", None)` when either bound has no in-window bar (or the
    entry bar is stale, when checked), else `("matured", {"entry",
    "entry_date", "exit", "exit_date", "value", "priced_as_of"})` with
    `value = exit / entry - 1.0`."""
    exit_date = decision + timedelta(days=horizon_days)
    if exit_date > datetime.now(timezone.utc).date():
        return "pending", None  # horizon hasn't elapsed yet
    # Batch-only path (scoreboard grading + forecast_resolver.resolve_due — no
    # interactive route calls this function): a local rate-limit hit waits
    # out the adapter's fixed Redis window rather than aborting the whole
    # grading run, bounded by `_RATE_LIMIT_WAIT_RETRIES`. The sleep happens
    # OUTSIDE `session_scope()` so no DB session is held while waiting.
    attempt = 0
    while True:
        adapter = build_adapter(source, redis, settings)
        try:
            async with session_scope() as session:
                env = await HistoryStore(session, redis, settings).get_daily(
                    adapter, symbol,
                    datetime.combine(
                        decision - timedelta(days=stale_days), datetime.min.time(), tzinfo=timezone.utc
                    ),
                    datetime.combine(
                        exit_date + timedelta(days=stale_days), datetime.min.time(), tzinfo=timezone.utc
                    ),
                )
            break
        except RateLimitExceeded:
            if attempt >= _RATE_LIMIT_WAIT_RETRIES:
                raise
            attempt += 1
            ttl = await redis.ttl(f"ratelimit:{adapter.name}")
            if ttl <= 0:
                ttl = 60
            await asyncio.sleep(ttl + 1)
    bars = sorted({c.ts.date(): float(c.close) for c in env.data}.items())
    entry_bar = next(((d, c) for d, c in bars if d >= decision), None)
    exit_bar = next(((d, c) for d, c in bars if d >= exit_date), None)
    entry_stale = (
        entry_stale_days is not None and entry_bar is not None
        and (entry_bar[0] - decision).days > entry_stale_days
    )
    if (
        entry_bar is None or exit_bar is None or entry_stale
        or (exit_bar[0] - exit_date).days > stale_days
        or entry_bar[1] <= 0
    ):
        return "uncovered", None
    entry_date, entry = entry_bar
    exit_date_actual, exit_close = exit_bar
    return "matured", {
        "entry": entry, "entry_date": entry_date.isoformat(),
        "exit": exit_close, "exit_date": exit_date_actual.isoformat(),
        "value": exit_close / entry - 1.0,
        "priced_as_of": datetime.now(timezone.utc).isoformat(),
    }


class ShadowEngine:
    """Deterministic per-tool shadow verdict mapper + statistical grader
   . See module docstring for the `readings` contract."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        # `(symbol, source)` negative memo for `SymbolNotFound` — reset at
        # the start of every `scoreboard()` run (per-build, not module-global)
        # so a dead symbol is priced at most once per build, not once per
        # verdict-change event.
        self._dead_symbols: set[tuple[str, str]] = set()

    # ── verdict mapping + idempotent upsert ─────────────────────────────
    async def compute_verdicts(
        self, report_week: str, readings: dict[str, dict[str, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        s = self._settings
        ce_symbols = s.advisor_cash_equivalent_symbols_set
        rows: list[dict[str, Any]] = []
        for tool, per_symbol in readings.items():
            if tool not in TOOLS or not per_symbol:
                continue
            # Cash-equivalent holdings (stablecoins) are treated as CASH
            # (mirrors advisor.py) — no tool ever emits a shadow verdict for
            # one, regardless of which reading dict handed it in.
            per_symbol = {sym: r for sym, r in per_symbol.items() if sym.upper() not in ce_symbols}
            if not per_symbol:
                continue
            mapped: dict[str, tuple[str, int | None, str]]
            if tool == "technicals":
                mapped = {
                    sym: _verdict_technicals(
                        r, s.report_pack_shadow_rsi_overbought, s.report_pack_shadow_rsi_oversold
                    )
                    for sym, r in per_symbol.items()
                }
            elif tool == "patterns":
                mapped = {sym: _verdict_patterns(r) for sym, r in per_symbol.items()}
            elif tool == "forensic":
                mapped = {
                    sym: _verdict_forensic(
                        r, s.report_pack_shadow_piotroski_weak, s.report_pack_shadow_piotroski_strong
                    )
                    for sym, r in per_symbol.items()
                }
            else:  # factor | signals — graded cross-sectionally, never on absolute price
                values = {
                    sym: float(r["value"]) for sym, r in per_symbol.items() if r.get("value") is not None
                }
                tercile = _tercile_verdicts(values)
                mapped = {
                    sym: tercile.get(sym, ("hold", None, "no value supplied")) for sym in per_symbol
                }
            for sym, (verdict, direction, rationale) in mapped.items():
                # `bar_ts` — the caller (report_worker's
                # `_shadow_readings`) MAY thread a transient `"bar_ts"` key
                # onto a per-symbol reading: the timestamp of the last daily
                # bar that reading was built from, used downstream as a
                # shadow-issued forecast's PIT `published_at` (M6 — NOT the
                # fetch time). Lifted onto the ROW (not left inside
                # `reading`) and stripped again before `_upsert` persists,
                # so `shadow_verdicts.reading` and `chapter_shadow()` stay
                # byte-identical to before this item.
                full_reading = per_symbol[sym]
                bar_ts = full_reading.get("bar_ts")
                reading = {k: v for k, v in full_reading.items() if k != "bar_ts"}
                rows.append({
                    "report_week": report_week, "symbol": sym.upper(), "tool": tool,
                    "verdict": verdict, "direction": direction,
                    "reading": reading, "rationale": rationale, "bar_ts": bar_ts,
                })
        rows.sort(key=lambda r: (r["tool"], r["symbol"]))  # deterministic regardless of input order
        await self._upsert(rows)
        return rows

    async def _upsert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        # `bar_ts` is transient (never persisted, see `compute_verdicts`);
        # `horizon_days` (Phase 18; F14) is the fixed grading
        # window every row written from here on carries — historic NULL
        # rows are read back as the same constant (see the ORM docstring).
        persist_rows = [
            {**{k: v for k, v in r.items() if k != "bar_ts"}, "horizon_days": _GRADE_HORIZON_WEEKS * 7}
            for r in rows
        ]
        async with session_scope() as session:
            stmt = insert(ShadowVerdict).values(persist_rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_shadow_verdicts_week_symbol_tool",
                set_={
                    "verdict": stmt.excluded.verdict,
                    "direction": stmt.excluded.direction,
                    "reading": stmt.excluded.reading,
                    "rationale": stmt.excluded.rationale,
                    "horizon_days": stmt.excluded.horizon_days,
                },
            )
            await session.execute(stmt)
            await session.commit()

    # ── history read (own async def so tests can monkeypatch just this) ──
    async def _load_history(self, report_week: str) -> list[dict[str, Any]]:
        async with session_scope() as session:
            res = (await session.execute(
                select(
                    ShadowVerdict.report_week, ShadowVerdict.symbol, ShadowVerdict.tool,
                    ShadowVerdict.verdict, ShadowVerdict.direction, ShadowVerdict.reading,
                )
                .where(ShadowVerdict.report_week <= report_week)
                .order_by(ShadowVerdict.tool, ShadowVerdict.symbol, ShadowVerdict.report_week)
            )).all()
        return [dict(r._mapping) for r in res]

    # ── 4-week-forward pricing (own async def; F0 HistoryStore, by symbol
    #    regardless of current holding status) — delegates to the
    #    module-level `fetch_forward_return`, fixing the
    #    scoreboard's own `stale_days` at 5 with `entry_stale_days=None`
    #    (legacy, pre-Phase-18 behaviour — no entry-side
    #    staleness check, byte-identical to the scoreboard's historic
    #    grading); the forecast resolver calls the module function
    #    directly with both `stale_days` AND `entry_stale_days` set to
    #    `settings.forecast_resolver_stale_days` instead ──────────────────
    async def _fetch_return(
        self, symbol: str, source: str, decision: date, horizon_days: int
    ) -> tuple[str, float | None]:
        status, d = await fetch_forward_return(
            self._redis, self._settings, symbol, source, decision, horizon_days,
            stale_days=5, entry_stale_days=None,
        )
        return status, (d["value"] if d else None)

    async def _grade_tool(
        self,
        tool: str,
        events: list[dict[str, Any]],
        first_week: str | None,
        report_week: str,
        symbols_at_latest: set[str],
    ) -> dict[str, Any]:
        s = self._settings
        cross_sectional = tool in ("factor", "signals")
        # Fixed 4-week forward window (`_GRADE_HORIZON_WEEKS`) — deliberately
        # decoupled from `report_pack_shadow_grade_min_weeks` (the calendar
        # maturity gate below); conflating the two made low_confidence
        # meaningless.
        horizon_days = _GRADE_HORIZON_WEEKS * 7
        # Calendar-time maturity gate (config's own definition: WEEKS of
        # shadow_verdicts history), not an event count — a tool held in one
        # unbroken verdict for 20 weeks is 1 event but 20 weeks of history.
        weeks_of_history = _weeks_between(first_week, report_week) + 1 if first_week else 0
        n_ungraded_null = n_pending = n_uncovered = 0
        observations: list[dict[str, Any]] = []
        full_cross_section: list[dict[str, Any]] = []
        for ev in events:
            is_null = ev["direction"] is None
            if is_null and not cross_sectional:
                n_ungraded_null += 1
                continue
            decision = _week_start(ev["report_week"])
            reading = ev.get("reading") or {}
            source = reading.get("source") or s.price_source_default
            memo_key = (ev["symbol"], source)
            if memo_key in self._dead_symbols:
                status, fwd = "uncovered", None
            else:
                try:
                    status, fwd = await self._fetch_return(ev["symbol"], source, decision, horizon_days)
                except CatallaxError as exc:
                    # A single unpriceable symbol or a limiter burst must not
                    # abort the whole pack (root cause of the W37+ pricing
                    # failures) — treat it exactly like an "uncovered" fetch.
                    logger.warning(
                        "shadow grading: pricing failed tool=%s symbol=%s source=%s code=%s error=%s",
                        tool, ev["symbol"], source, getattr(exc, "code", None), exc,
                    )
                    if isinstance(exc, SymbolNotFound):
                        self._dead_symbols.add(memo_key)
                    status, fwd = "uncovered", None
            if status == "pending":
                n_pending += 1
                if is_null:
                    n_ungraded_null += 1
                continue
            if status == "uncovered":
                n_uncovered += 1
                if is_null:
                    n_ungraded_null += 1
                continue
            # matured: infer "exited" at grading time (no producer sets it) —
            # a symbol with history but absent from the tool's LATEST verdict
            # week left the book; an explicit reading["exited"] still wins.
            exited = bool(reading["exited"]) if "exited" in reading else ev["symbol"] not in symbols_at_latest
            item = {
                "symbol": ev["symbol"], "report_week": ev["report_week"],
                "direction": ev["direction"], "forward_return": fwd, "exited": exited,
            }
            if cross_sectional:
                full_cross_section.append(item)
            if is_null:
                n_ungraded_null += 1
            else:
                observations.append(item)
        return _score_tool(
            tool, len(events), n_ungraded_null, n_pending, n_uncovered,
            observations, full_cross_section, weeks_of_history,
            s.report_pack_shadow_grade_min_weeks, horizon_days,
        )

    # ── graded scoreboard payload ────────────────────────────────────────
    async def scoreboard(self, report_week: str) -> dict[str, Any]:
        priced_as_of = datetime.now(timezone.utc).isoformat()
        self._dead_symbols = set()  # per-build memo, not module-global
        rows = await self._load_history(report_week)
        events = _collapse_events(rows)
        # Earliest shadow_verdicts row per tool (across all symbols) — the
        # calendar-history anchor for the low-confidence gate, independent
        # of how the verdicts collapsed into change-events. Also the latest
        # verdict week's roster per tool, to infer "exited" symbols (left
        # the book) without relying on a producer-supplied flag.
        first_week_by_tool: dict[str, str] = {}
        week_symbols_by_tool: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for r in rows:
            t = r["tool"]
            if t not in first_week_by_tool or r["report_week"] < first_week_by_tool[t]:
                first_week_by_tool[t] = r["report_week"]
            week_symbols_by_tool[t][r["report_week"]].add(r["symbol"])
        symbols_at_latest_by_tool: dict[str, set[str]] = {
            t: weeks[max(weeks)] for t, weeks in week_symbols_by_tool.items() if weeks
        }
        tools_out: dict[str, Any] = {}
        k = 0
        for tool in TOOLS:
            tool_events = events.get(tool, [])
            if tool == "forensic":
                # Never enters the graded denominator.
                tools_out[tool] = {
                    "n_events": len(tool_events), "graded": False, "reason": _FORENSIC_HORIZON_NOTE,
                }
                continue
            result = await self._grade_tool(
                tool, tool_events, first_week_by_tool.get(tool), report_week,
                symbols_at_latest_by_tool.get(tool, set()),
            )
            result["graded"] = True
            tools_out[tool] = result
            if result.get("hit_rate") is not None:
                k += 1
        return {
            "report_week": report_week,
            "priced_as_of": priced_as_of,
            "nature": NATURE,
            "grade_min_weeks": self._settings.report_pack_shadow_grade_min_weeks,
            "k": k,
            "deflation_note": _deflation_note(k) if k > 1 else None,
            "tools": tools_out,
        }

    # ── chapter assembler (sections key "shadow") ────────────────────────
    async def chapter_shadow(self, report_week: str) -> dict[str, Any]:
        board = await self.scoreboard(report_week)
        return {
            "engine": "shadow-verdict-engine-v1",
            "how_to_read": (
                "Every row is a deterministic tool→direction mapping graded against what "
                "actually happened afterward — never a recommendation. A verdict counts once "
                "per CHANGE (a flag re-emitted every week is one event; \"hold\" weeks never "
                "split or restart one). Absolute-direction tools (technicals, patterns) show "
                "a benchmark that is the SAME graded sample's own unconditional up-rate, not "
                "the whole book's — a raw hit-rate still can't be read as edge against it "
                "alone. The cross-sectional tools (factor, signals) grade a relative pick "
                "against the median forward return of the full covered cross-section that "
                "week (every matured name, not just the graded picks); a pick landing exactly "
                "on that median is a push, excluded from both hits and misses. Because a "
                "week's tercile pairs one add_watch against one trim_watch from the same "
                "ranking, cross-sectional Wilson lower bounds are computed on an `effective_n` "
                "(graded N halved) rather than the raw graded count. `priced_as_of` timestamps "
                "when the forward returns behind this table were fetched. Forensic scores are "
                f"shown but never graded — their {_FORENSIC_HORIZON_NOTE}. Cash-equivalent "
                "holdings (stablecoins) are treated as cash, never as a gradeable position — "
                "no tool emits a verdict for them."
            ),
            **board,
        }

    # ── calibration read (own async def so tests can monkeypatch just this) ──
    async def _load_calibration_rows(self, floor_date: date, ceiling_date: date) -> list[dict[str, Any]]:
        """`forecasts` LEFT JOIN `outcomes`, `decision_date` in
        `[floor_date, ceiling_date]` (F15's calendar window). A `pending`
        forecast has no `outcomes` row (`Outcome.hit` etc. come back NULL
        from the LEFT JOIN) and is reported back tagged `"status": "pending"`
        with nothing else — `calibration()` counts those separately rather
        than folding them into the graded `observations`."""
        async with session_scope() as session:
            rows = (await session.execute(
                select(
                    Forecast.issued_by, Forecast.horizon_days, Forecast.probability,
                    Forecast.status, Forecast.target, Outcome.hit, Outcome.resolution_status,
                    Outcome.realized,
                )
                .outerjoin(Outcome, Outcome.forecast_id == Forecast.id)
                .where(Forecast.decision_date >= floor_date, Forecast.decision_date <= ceiling_date)
            )).all()
        out: list[dict[str, Any]] = []
        for issued_by, horizon_days, probability, status, target, hit, resolution_status, realized in rows:
            if status == "pending":
                out.append({"issued_by": issued_by, "horizon_days": horizon_days, "status": "pending"})
                continue
            realized = realized or {}
            metric_value = realized.get("value")
            if (target or {}).get("metric") == "excess_return_vs_book_median":
                book_median = realized.get("book_median")
                metric_value = (
                    metric_value - book_median
                    if metric_value is not None and book_median is not None else None
                )
            out.append({
                "issued_by": issued_by, "horizon_days": horizon_days, "status": status,
                "p": probability, "hit": hit, "abstained": resolution_status == "uncovered",
                "benchmark_hit": (metric_value > 0) if metric_value is not None else None,
            })
        return out

    # ── calibration payload (sections key "calibration") ─────────────────
    async def calibration(self, report_week: str) -> dict[str, Any]:
        """Grades the FORECAST LEDGER (`forecasts` ⋈ `outcomes`), one
        row per issued forecast rather than per verdict-change event — see
        module docstring "Calibration" section. Groups by `issued_by` ×
        `horizon_days` over the last `report_pack_calibration_window_weeks`
       . `bins` (B2): computed only when the group's DECIDED
        probabilities span >= 2 equal-width bins — a machine issuer's
        near-constant per-week Laplace prior makes a reliability histogram
        tautological; those groups get `bins=None` + `bins_note` and read
        discrimination (hit_rate vs benchmark_hit_rate) + `brier_skill`
        instead. `groups` is a LIST."""
        s = self._settings
        n_bins = s.report_pack_calibration_bins
        min_n = s.forecast_probability_min_n
        priced_as_of = datetime.now(timezone.utc).isoformat()
        ceiling = _week_start(report_week)
        floor = ceiling - timedelta(weeks=s.report_pack_calibration_window_weeks)
        rows = await self._load_calibration_rows(floor, ceiling)

        graded: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        pending: dict[tuple[str, int], int] = defaultdict(int)
        for r in rows:
            key = (r["issued_by"], r["horizon_days"])
            if r["status"] == "pending":
                pending[key] += 1
            else:
                graded[key].append(r)

        groups_out: list[dict[str, Any]] = []
        summaries: dict[tuple[str, int], dict[str, Any]] = {}
        for key in sorted(set(graded) | set(pending)):
            issued_by, horizon_days = key
            tool = issued_by.split(":", 1)[1] if issued_by.startswith("shadow:") else None
            cross_sectional = tool in ("factor", "signals")
            observations = graded.get(key, [])
            summary = _calibration_summary(observations, cross_sectional=cross_sectional)
            summaries[key] = summary
            decided = [o for o in observations if not o.get("abstained") and o.get("hit") is not None]
            spanned_bins = {_bin_index(o["p"], n_bins) for o in decided}
            if len(spanned_bins) >= 2:
                bins, bins_note = _reliability_bins(decided, n_bins), None
            else:
                bins, bins_note = None, _CALIBRATION_BINS_NOTE
            groups_out.append({
                "issued_by": issued_by, "horizon_days": horizon_days, "summary": summary,
                "bins": bins, "bins_note": bins_note, "pending": pending.get(key, 0),
                "priced_as_of": priced_as_of,
                "low_confidence": (summary["effective_n"] or 0) < min_n,
            })

        k = sum(1 for s_ in summaries.values() if s_.get("hit_rate") is not None)
        note = _deflation_note(k) if k > 1 else None
        for g in groups_out:
            g["deflation_note"] = note

        return {
            "engine": "calibration-v1",
            "groups": groups_out,
            "how_to_read": (
                "Grades the forecast ledger (issued probability vs realised outcome), one row "
                "per issued forecast, complementing the shadow scoreboard above."
            ),
        }

    # ── chapter assembler (sections key "calibration") ────────────────────
    async def chapter_calibration(self, report_week: str) -> dict[str, Any]:
        board = await self.calibration(report_week)
        return {
            **board,
            "how_to_read": (
                "Each row grades one issuer's forecasts at one horizon against what actually "
                "happened, via the Brier score (mean squared error of the issued probability "
                "against the 0/1 outcome — lower is better) and Brier skill (`1 - brier_mean / "
                "brier_ref`, positive means better than the group's OWN base rate). The "
                "reference is that base rate (`p̄(1-p̄)`, the climatological forecaster), never "
                "a flat 0.25 coin — the coin is shown alongside only as a labelled second "
                "column. Machine issuers (shadow:<tool>) show NO reliability bins: their "
                "probability is a Laplace rule-of-succession estimate over their own track "
                "record (seeded from the shadow scoreboard), so it barely varies week to week "
                "and a bin histogram over it would be tautological — read discrimination "
                "(hit_rate vs benchmark_hit_rate) and Brier skill instead. Human-issued "
                "forecasts vary probability by call and so get bins. The first weeks after "
                "issuance show `n=0` honestly — a forecast only enters this table once its "
                "outcome resolves. Because probabilities start as a Laplace prior, early Brier "
                "is EXPECTED to sit near `p̄(1-p̄)` (skill near 0) regardless of true skill — "
                "read `low_confidence` before crediting or blaming an issuer. Cross-sectional "
                "issuers (factor, signals) count observations at half weight (`effective_n`), "
                "mirroring the shadow scoreboard's own tercile-pairing rule. `abstained` "
                "forecasts (no priceable outcome) correlate with delisting/exit — a "
                "survivorship-biased sample, not a random one; do not read `abstention_rate` "
                "as if it were missing-at-random."
            ),
        }
