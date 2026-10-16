"""Weekly report pack PDF pipeline (core: Docker image, Jinja2
template, render module; the `/pdf` route in `routers/reports.py` is a
separate task and consumes `render_pack_pdf`/`resolve_pdf_path` from here).

`sections` dict contract:
`exec_summary`, `portfolio`, `market`, `holdings`, `quant`, `shadow`,
`methodology`. Every present chapter carries `engine` + `how_to_read`;
an absent chapter is `{"absent": True, "reason": str}`.

Fix round 3 (shape alignment): the per-chapter
body fields below were ORIGINALLY THIS MODULE'S OWN GUESS, invented before
`services/report_pack.py`, `services/shadow.py` and `app/report_worker.py` existed. Those are now built and are the source of
truth — this module adapts to them, never the other way (they are
read-only from here). Real shapes, read from that code directly:
  exec_summary (assembled in report_worker.py `_assemble_exec_summary`):
    nav_eur, wow_pct, regime (a DICT — `market.regime`, e.g. `{"state":...,
    "score":...}`, NOT a string — `_cover()` reads `regime["state"]`),
    chapters_present{}, n_chapters_present, shadow_counts{}, headline,
    highlights[], narration (str|None — plain text, rendered escaped, never
    `|safe`), narration_absent_reason (str|None — shown small+muted when
    `narration` is absent; fix round 4).
  portfolio (report_pack.py `chapter_portfolio`): nav_eur, cash_eur,
    positions[{symbol,qty,value_eur,weight_pct,wow_value_delta_eur,
    wow_weight_delta_pct}] (NOT `wow_delta_pct`), var_cvar{} (risk bundle's
    `var` — real leaf fields per `services/risk.py`'s `_var_block`/bundle:
    var_eur/var_pct/cvar_eur/cvar_pct/var99_eur/cvar99_eur/fat_tail_ratio/
    worst_day/worst_day_pct/confidence/low_confidence/n_scenarios, plus a
    NESTED `coverage{}` — n_assets_total/n_covered/n_skipped/
    covered_weight_pct/n_observations/window_start/window_end/
    cash_weight_pct/assumption — assumption is already English prose, meant
    to be typeset directly, never dict-dumped), scorecard{} (frozen
    `compute_scorecard()` output — `actual_twr`/`do_nothing_twr`/
    `depth_weeks`/`n_effective`/`p_floor_two_sided`/`convention`/`empty`/
    `note`; `depth_weeks < 2` or `empty` is the documented "insufficient
    history" state — no historical NAV series ANYWHERE in this payload,
    hence the "NAV/scorecard lines" chart is the two-arm cumulative TWR
    path, not an actual NAV history). NO `allocation`/`nav_history` fields
    exist — the allocation donut is derived from `positions[]`.
  market (report_pack.py `chapter_market`): regime (RegimeReport:
    state/score/components[{key,label,value,score,weight,contribution}]/
    history[{date,score}]), cross_asset (CrossAssetReport: symbols[],
    snapshot[{symbol,label,last,chg_1d_pct,chg_1w_pct,chg_1m_pct,
    chg_ytd_pct}], matrix (symbols x symbols correlation), avg_pairwise_corr),
    rotation/rrg (not surfaced in the redesigned pack — outside rule 7's
    stated scope, and not promised anywhere else either, so their absence
    is silent rather than a residual "N fields recorded" note, per the
    design review fix round),
    volatility/breadth/sentiment/valuation (heterogeneous per-domain dicts,
    see `services/market_research.py`'s Pydantic report models for the real
    leaf names), econ_calendar_week_ahead (an Envelope's `model_dump`:
    `{"data": {"events": [...], "needs_key": bool}, "source": str,
    "stale": bool}`).
  holdings (report_pack.py `chapter_holdings`): `symbols` — a DICT KEYED BY
    SYMBOL (NOT a `holdings[]` list), each entry
    `{fundamentals{},valuation{},forensic{cards[...]},technicals{series:
    [{date,close,sma50,sma200}],rsi_14,trend,...},scan{n_hits,...},news[...]}`
    or `{"absent": True, "reason": str}` per-symbol; chapter-level
    `symbols_failed{symbol:reason}`/`n_symbols_failed` is the partial-failure
    marker (per-symbol, additive — never folds into a hard chapter absence).
    No `score`/`sparkline` field exists per entry — the sparkline is
    `technicals.series[].close`; the "score tile" is the Piotroski card's
    `score` off `forensic.cards` (`_holding_scores()`).
  quant (report_pack.py `chapter_quant` + report_worker.py's factor payload):
    `factor` — a `FactorReport`-shaped dict (`ic_series[{date,ic}]`,
    `quantile_returns[{quantile,mean_fwd_ret_pct,n_avg}]`, `ic_mean`,
    `ic_ir`, `n_symbols`, `stale` — the stale-fallback marker, ...) or
    `{"absent": True, "reason": str}`; NOT a `factor_ic[]` list. `scoreboard`
    — a `Scoreboard`-shaped dict (`rows[{signal_id,mean_rank_ic,hit_rate,
    significant,methodology,...}]`), NOT a `signal_scoreboard[]` list. No
    top-level `universe_size` — `factor.n_symbols` is the closest analog.
  shadow (shadow.py `chapter_shadow`): `nature`, `k`, `deflation_note`,
    `priced_as_of` (an ISO timestamp — table-level, not per-tool; fix round 5
    trims it to seconds for the caption, mirroring the frontend widget's
    convention), `tools` — a DICT KEYED BY TOOL NAME (NOT a `scoreboard[]`
    list), each `{n_events,n_graded,hit_rate,benchmark_hit_rate,
    hit_rate_wilson_lb,effective_n,pushes,low_confidence,warnings[],graded,
    ...}` (forensic: `{n_events,graded:false,reason}`).
  methodology (report_pack.py `chapter_methodology`): `version`,
    `tools[{tool,measures,endpoint,limitations,grading_horizon}]` — field
    names `tool`/`measures`, NOT `name`/`what_it_measures`.

The template renders untrusted, web-sourced strings (news titles, etc.)
verbatim through Jinja variables — `autoescape=True` (set directly, not
inferred by extension) is what makes that safe; never construct a
`jinja2.Environment` for this template without it.

Fix round: the render path must
never raise just because a chapter's data is PRESENT but wrong-shaped (e.g.
a list field arriving as a string — a plausible upstream engine bug, not
just a missing-field case). `_normalize_sections` runs an isinstance-guarded
pass over every known structured (list/dict) field the template and
`_extract_chart_data` iterate, BEFORE either sees the data; a wrong-shaped
field is dropped (treated as missing) and named in a `render_warnings` list.
`render_pack_pdf`'s return type is a fixed cross-task interface contract (the report worker
depends on it) and is deliberately left unchanged — warnings are (a) logged
via the module logger and (b) rendered into the PDF itself as a small
"Render Warnings" appendix section, so a degraded render is visible rather
than silently wrong, without adding a second return channel.

Fix round 2: the same
guard extends to numeric LEAVES (any field the template feeds through a
numeric `format`/`eur` filter), not just containers — `_normalize_sections`
coerces every such field via `float(x)` (accepting numeric strings, e.g.
`"42.5"`), replacing a non-coercible value with `None` (+ a warning) so the
template's pre-existing `is not none else <em-dash>` guards actually hold.

Fix round 3 (shape alignment): all of the above guard machinery is
preserved; only the FIELD NAMES/PATHS the tables and `_extract_chart_data`
point at changed, to match the real producer output documented above
instead of this module's original invented contract.

Fix round 4 (PDF honesty): three SEV1s. (1) The shadow table printed n_events/hit_rate
without n_graded (the real denominator), inviting the reader to conflate
the two — now prints `n_graded` too, plus `effective_n`/`pushes` when the
shadow owner's payload carries them (both read defensively via `.get()` —
optional fields, not yet guaranteed present), and every tool's
`low_confidence`/`warnings[]` render VISIBLY under the table (not a
footnote glyph). (2) `quant.factor.warnings[]` and
`quant.scoreboard.warnings[]` were guarded (isinstance-checked) but never
actually RENDERED — silently dropped caveats (e.g. "today's holdings
applied to past decision dates ... not research-grade edge") now print
verbatim next to the numbers they qualify. (3) `exec_summary.narration`
(plain string, escaped — no `|safe` anywhere, autoescape alone is what
makes this safe) and `narration_absent_reason` (shown small+muted when
narration is absent) are new string-scalar fields —
`_CHAPTER_STRING_SCALAR_FIELDS`/`as_str()` extend `_normalize_sections`
with a THIRD leaf-type guard (containers, numeric leaves, now string
leaves), same drop-to-None-plus-warning discipline as the other two.

Fix round 5 (editorial redesign, renderer-side only — the `sections` INPUT
SHAPE above is unchanged; this is presentation, not contract): the 42-page
data dump is replaced with a ≤16-page institutional weekly note. The
generic key/value fallback (rule the brief calls out by name) is GONE from
every known chapter — `market`'s regime/cross-asset/volatility/breadth/
sentiment/valuation/econ-calendar all get bespoke, typeset renderers
(`_regime_components_view`/`_econ_calendar_rows` etc.) instead of a raw
`chapter.items()` dump; the
positions table collapses to the top holdings by weight (`_split_positions`)
with a single "Tail: N positions, €X, Y% of NAV" row; the risk/coverage/
scorecard dicts are read through named leaves (var_eur, cvar_eur, ...) and
a Python-built prose sentence (`_coverage_sentence`), never printed as a
dict. New sections: a masthead cover with a KPI band + page-less TOC
(`_week_date_range`), an "In plain terms" panel translating the headline
figures for a non-quant reader (`_plain_terms` — every sentence is
value-gated: a missing input drops that sentence, never invents one), and a
methodology "Conventions" subsection that typesets the risk/scorecard
modules' own English-prose `assumption`/`convention` strings
(`_methodology_conventions`) instead of dumping the dicts that carry them.
Two new Jinja filters, `pct`/`num` (registered in `_environment`), replace
the repeated `"%.1f%%"|format(x) if x is not none else "—"` boilerplate
throughout the template — both are None-safe (the `scale` kwarg multiplies
AFTER the None-check, so a fraction-scale field like `hit_rate` can be
formatted with `| pct(1, false, 100)` without a separate Jinja-side `if`
guard around the multiply). Every new helper here is defensively
isinstance-guarded and returns an empty/None result on malformed input,
matching the existing tolerance style of `_extract_chart_data` — a
malformed `market` sub-block simply drops that block's row/line rather than
raising or falling through to a repr dump. The formal
`_normalize_sections`/`render_warnings` machinery is EXTENDED (not
replaced) to the new numeric leaves this redesign reads out of
`var_cvar`/`var_cvar.coverage`/`scorecard`, since those now feed the same
class of numeric Jinja filters the fix-round-2 guard exists for.

Fix round 6:
P0 — `_holdings_view` had a silent-drop bug: a MATERIAL position with no
`holdings.symbols[symbol]` entry AT ALL (not even a per-symbol `absent`
placeholder) fell through both the card path and the roster path and never
rendered anywhere; it now gets a synthetic absent slot
(`no_data_recorded=True`) — see `_holdings_view`'s docstring. P1s: `_quantile_returns_view` collapses the quantile-returns
table to one sentence when every bucket's forward return is null (a
degenerate universe, not "zero returns"); the render container only has
`Liberation Serif` installed (Georgia silently falls back) — CSS now
declares it explicitly first, `tabular-nums` on numeric table cells, and the
whole type scale is bumped ~15-20% (the page-count budget had headroom);
`vendor/charts.js` gained x-axis date labels for the regime-history line and
factor-IC bars (the chart-data JSON already carried the dates; the renderer
just never painted them) plus a min/max annotation on the regime line;
`.kv`/`.stat-row` rows get a `gap` + fixed label column so a long
"Limitations" line can't collide with its value; `break-after: avoid` on
h3/h4 and `orphans`/`widows` on tables address heading/row orphans at page
boundaries; the shadow chapter's per-tool warning lines collapse to one
banner + genuine per-tool exceptions (`_shadow_warnings_view`), with
`effective_n`/`pushes` promoted from prose into scoreboard table columns;
the week-ahead calendar ranks by keyword importance tier instead of feed
order and shows at most 8 tier-1/2 rows (`_calendar_tier`); the regime
components footnote states inline that the score is renormalized over
available components when any are missing; holdings news headlines get a
muted "· substring match" suffix. P2s: the donut palette is 8 distinct hues
+ a fixed neutral grey for "Other" (was cycling through 7, colliding at the
8th real slice); counts pluralize via the new `_plural` helper/`|plural`
filter; the positions table drops its WoW columns (one caption line
instead) when every shown row has no WoW figure at all, same
collapse-when-absent pattern as the scorecard; the "In plain terms" panel's
fat-tail sentence is reworded in genuinely plain language; every holding
card renders a fixed slot skeleton (price/trend/RSI/score, em-dash for
missing) instead of omitting rows with no data, so cards don't jump around
in shape. The market chapter's prior "N further field(s) recorded" residual
note is REMOVED (not replaced) — it sat directly under
the calendar table with no visual separation and read as if it were about
hidden calendar rows rather than the actually-unrelated, always-out-of-scope
rotation/rrg fields; see the comment above `_correlation_pairs` for why that
residual note class doesn't apply to rotation/rrg the way it still would to
a genuine broken promise (contrast the P0 holdings fix, which IS a broken
promise and gets an explicit slot, not a removed note).

Feature (per-chapter "In plain terms" boxes, extends rule 3): the Executive
Summary's plain-terms box was the ONLY translation panel — every other
analytical chapter (Portfolio Review / Market Context / Holdings Deep-Dive /
Quant / Shadow-Mode Tool Grading) now gets its own, placed at the END of the
chapter (after the data, so it reads as interpretation of what was just
shown, not a preview) via a shared Jinja macro (`plain_terms_box` in
`report_pack.html`) so the markup/style never drifts between the six boxes.
Same value-gated-sentence discipline as the original: `_plain_terms_portfolio`
/ `_plain_terms_market` / `_plain_terms_holdings` / `_plain_terms_quant` /
`_plain_terms_shadow` each read ONLY real fields off their own already-
normalized chapter dict and drop a sentence outright when its input is
missing — nothing is ever invented. Methodology gets no box (it IS the
explainer). Two small helpers were factored out of the original
`_plain_terms` so wording is shared, not duplicated: `_var95_sentence` (the
VaR95 "everyday" sentence, identical between the exec-summary panel and
Portfolio Review's own panel) and `_shadow_definition_sentence` (what shadow
grading is, with an optional "(Chapter N)" pointer used only by the exec
summary's cross-reference). Holdings' box explains what the three forensic
scores measure in one sentence each (unconditional — explaining a
methodology isn't a value derived from this week's data) then reports
value-gated findings (strongest/weakest Piotroski, any Altman distress-zone
name, a Beneish manipulation-flag line, and — since crypto carries no
forensic scores by design — an explicit "that's expected, not missing data"
sentence when any covered holding is crypto).

Phase 18 (Forecasts + Calibration chapters, additive): `_CHAPTER_ORDER`
gains `forecasts` and `calibration` right after `shadow`; the stored
`sections["decisions"]` chapter is deliberately NOT added here (Phase
19 — the Decisions table is out of scope this phase) and is silently
ignored by the render loop, same as any other unknown key. Real shapes
(`report_worker.py::_issue_forecasts` / `shadow.py::calibration`,
`chapter_calibration`), read-only from here:
  forecasts: `engine`, `how_to_read`, `issued`, `duplicates`, `skipped`
    (`{"covered", "ungradeable"}`), `issuance_errors`
    (`[{"issued_by","subject","reason"}]`), `pending_total`, `next_due`
    (ISO date or `None`) — or the wrapped absent form
    `{"engine":..., "absent": True, "reason": str}` the worker writes on an
    issuance failure (existing generic `chapter.get("absent")` handling).
  calibration: `engine`, `how_to_read`, `groups` — a LIST (never a dict
    keyed by issuer), each `{"issued_by", "horizon_days", "summary" (the
    `_calibration_summary` dict — `n`/`effective_n`/`brier_mean`/
    `brier_ref`/`brier_coin`/`benchmark_n`/`benchmark_brier`/`brier_skill`/
    `log_score_mean`/`hit_rate`/`benchmark_hit_rate`/`wilson_lb`/`pushes`/
    `abstained`/`abstention_rate`), "bins" (a list of `_reliability_bins`
    rows, or `None` when the issuer's probability is a near-constant
    track-record prior — B2), "bins_note" (set iff `bins is None`),
    "pending", "priced_as_of", "low_confidence", "deflation_note"}`. A
    build failure drops the KEY entirely (chapter absent from `sections`,
    per the worker's own comment) rather than writing an absent stand-in —
    the render loop's `sections.get(key) or {}` already treats a missing
    key as an empty, non-absent chapter with nothing to show.
  `_calibration_groups_view`/`_plain_terms_calibration` render these two
  chapters; `_normalize_sections` gets matching entries in the per-chapter
  field tables plus one bespoke nested block (`groups[].summary`/`.bins`
  numeric leaves), same discipline as every other chapter above.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from app.services.markets import _CRYPTO as _MARKETS_CRYPTO

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Controller-pinned chapter order + display titles.
_CHAPTER_ORDER = [
    ("exec_summary", "Executive Summary"),
    ("portfolio", "Portfolio Review"),
    ("market", "Market Context"),
    ("holdings", "Holdings Deep-Dive"),
    ("quant", "Quant"),
    ("shadow", "Shadow-Mode Tool Grading"),
    # Phase 18 (additive): forecast-ledger issuance + calibration
    # grading, right after the shadow scoreboard they extend. `decisions`
    # is deliberately absent from this list — Phase 19 — and is
    # silently ignored by the render loop like any other unknown key.
    ("forecasts", "Forecasts Issued"),
    ("calibration", "Calibration"),
    ("methodology", "Methodology Appendix"),
]

# Positions at/above this weight (as a percent, e.g. 1.0 == 1%), OR the top
# `_TOP_POSITIONS_MIN_N` by weight — whichever set is LARGER — are "material"
# (rule 4 of the redesign brief): they get a positions-table row AND a
# holdings tear-sheet card. Everything else collapses to one tail row / one
# roster row. Shared by `_split_positions` (positions table) and
# `_holdings_view` (tear sheets) so the two never disagree on materiality.
_MATERIAL_WEIGHT_PCT = 1.0
_TOP_POSITIONS_MIN_N = 12
_ALLOCATION_DONUT_MAX_SLICES = 8

_logger = logging.getLogger(__name__)

# Per-chapter known structured (list/dict) fields the template + chart
# extraction iterate — the normalization pass below guards exactly these.
# Nested paths (e.g. `quant.factor.ic_series`, `holdings.symbols[sym].*`)
# aren't representable in a flat per-chapter table — those are guarded in
# the bespoke `holdings`/`quant`/`shadow`/`portfolio` blocks inside
# `_normalize_sections`. `market` is deliberately absent from every one of
# these per-chapter tables (see fix round 5): it has no fixed shape upstream
# (a Pydantic-report-per-domain bundle, not one flat dict) and every reader
# in this module treats it as untrusted/optional via isinstance guards at
# the point of use instead, matching `_extract_chart_data`'s existing
# tolerance style rather than extending this leaf-by-leaf table to dozens of
# nested paths across five independent report models.
_CHAPTER_LIST_OF_DICT_FIELDS: dict[str, tuple[str, ...]] = {
    "exec_summary": (),
    "portfolio": ("positions",),
    "quant": (),
    "holdings": (),
    "shadow": (),
    # Phase 18: `groups` is pinned as a LIST upstream — top-level
    # shape guarded here, nested `summary`/`bins` numeric leaves guarded in
    # the bespoke `calibration` block below (same split as `quant`/`shadow`).
    "calibration": ("groups",),
    "methodology": ("tools",),
}
_CHAPTER_SCALAR_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "exec_summary": ("highlights",),
    "forecasts": ("issuance_errors",),
}
_CHAPTER_DICT_FIELDS: dict[str, tuple[str, ...]] = {
    "portfolio": ("var_cvar", "scorecard"),
    "quant": ("factor", "scoreboard"),
    "holdings": ("symbols",),
    "shadow": ("tools",),
    "forecasts": ("skipped",),
}
_HOLDING_DICT_FIELDS = ("fundamentals", "valuation", "forensic", "technicals", "scan")
_HOLDING_SCALAR_LIST_FIELDS = ("news",)

# Every field the template feeds through a numeric filter (`|pct`/`|num`/
# `|eur`) — enumerated by grepping the template for those three filters, not
# just from any one reviewer finding.
_CHAPTER_NUMERIC_SCALAR_FIELDS: dict[str, tuple[str, ...]] = {
    "exec_summary": ("nav_eur", "wow_pct"),
    "forecasts": ("issued", "duplicates", "pending_total"),
}
# Phase 18: `groups[].summary` numeric leaves — guarded via the bespoke
# `calibration` block in `_normalize_sections` (nested one level inside an
# already-list-of-dicts-guarded field, same split as `var_cvar`/`coverage`).
_CALIBRATION_SUMMARY_NUMERIC_FIELDS = (
    "n", "effective_n", "brier_mean", "brier_ref", "brier_coin", "benchmark_n", "benchmark_brier",
    "brier_skill", "log_score_mean", "hit_rate", "benchmark_hit_rate", "wilson_lb",
    "pushes", "abstained", "abstention_rate",
)
_CALIBRATION_BIN_NUMERIC_FIELDS = ("lo", "hi", "n", "mean_p", "hit_rate", "wilson_lb")
# chapter -> {list field -> numeric field names on each item in that list}
_LIST_ITEM_NUMERIC_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "portfolio": {"positions": ("value_eur", "weight_pct", "wow_weight_delta_pct", "wow_value_delta_eur")},
}
# Redesign (fix round 5): the risk/coverage numeric leaves the new Risk
# section + "In plain terms" panel read directly off `var_cvar`/
# `var_cvar.coverage` and `scorecard` — same numeric-leaf guard discipline
# as the table above, applied via the bespoke `portfolio` block below
# because both are nested one level inside an already-dict-guarded field.
_VAR_CVAR_NUMERIC_FIELDS = (
    "var_eur", "var_pct", "cvar_eur", "cvar_pct", "var99_eur", "cvar99_eur",
    "fat_tail_ratio", "worst_day_pct", "confidence",
)
_VAR_CVAR_COVERAGE_NUMERIC_FIELDS = (
    "n_assets_total", "n_covered", "n_skipped", "covered_weight_pct",
    "n_observations", "cash_weight_pct",
)
_SCORECARD_NUMERIC_FIELDS = ("depth_weeks", "n_effective", "actual_twr", "do_nothing_twr", "p_floor_two_sided")

# String-scalar fields (fix round 4) — rendered as plain escaped text (never
# `|safe`), so they don't need a "format filter" guard, but a wrong TYPE
# (e.g. narration arriving as a dict) is still worth dropping-with-warning
# rather than dumping a Python repr into the PDF.
_CHAPTER_STRING_SCALAR_FIELDS: dict[str, tuple[str, ...]] = {
    "exec_summary": ("narration", "narration_absent_reason"),
    "forecasts": ("next_due",),
}

# Categorical ramp for the allocation donut ONLY — 8 distinct hues, none of
# them the reserved red/green (those stay meaningful-only, for deltas/figure
# sign, per the design register). Each donut slice's color is now assigned
# explicitly, in Python, and threaded through `_extract_chart_data`'s chart
# spec AS DATA (`"color"` per slice) — `vendor/charts.js`'s `drawDonut` reads
# `d.color` directly instead of re-deriving it by cycling its own PALETTE by
# index, so there is exactly one source of truth and no "kept in sync by
# hand" hazard (design review P2.12: with only 7 colors the ramp used to
# repeat at the 8th slice, so two different holdings shared one hue). "Other"
# ALWAYS gets the fixed neutral grey below, never a ramp color, regardless
# of how many real positions preceded it — a design review fix, not a
# cosmetic preference: a recycled hue on "Other" reads as "same holding
# twice", the one collision this fix must never reintroduce.
_PALETTE_HEX = [
    "#1b3a5c", "#3d6d9e", "#7ba7cc", "#c98a3a", "#6b4c9a", "#4c9a8b", "#9a6b4c", "#8a7a3a",
]
_PALETTE_OTHER_HEX = "#999999"

_HEADER_TEMPLATE = """
<div style="font-size:8px; width:100%; text-align:center; color:#555;
  padding-top:4px; font-family:'Liberation Serif',Georgia,'Times New Roman',serif;">
  Catallax &mdash; Weekly Report Pack &middot; {week}
</div>
"""

_FOOTER_TEMPLATE = """
<div style="font-size:8px; width:100%; text-align:center; color:#555;
  padding-bottom:4px; font-family:'Liberation Serif',Georgia,'Times New Roman',serif;">
  Page <span class="pageNumber"></span> of <span class="totalPages"></span>
</div>
"""


def resolve_pdf_path(path: str, base: str = "/data/reports") -> str:
    """Realpath-containment check: returns the resolved real path if it sits
    under `base`, else raises `ValueError`. Defense-in-depth against a
    traversal/symlink escape reaching `FileResponse` in the (separately
    owned) `/pdf` route — the value itself is worker-written, but the check
    is two lines and this is where both writer and reader import it from."""
    real_base = os.path.realpath(base)
    real_path = os.path.realpath(path)
    if real_path != real_base and not real_path.startswith(real_base + os.sep):
        raise ValueError(f"path {path!r} resolves outside report base {base!r}")
    return real_path


def _fmt_eur(value: Any) -> str:
    try:
        return "€{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "—"


def _spark_key(symbol: Any) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(symbol)).strip("_").lower()
    return f"holding_spark_{slug or 'na'}"


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _num_or_none(value: Any) -> float | None:
    """Like `_num`, but a non-coercible value stays `None` instead of
    silently becoming 0.0 — for anywhere a missing figure must render as an
    em-dash, not a fabricated zero (chart summation is the one place `_num`'s
    0.0 fallback is correct; every DISPLAY path below wants this instead)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: Any, nd: int = 1, sign: bool = False, scale: float = 1.0) -> str:
    """Jinja filter `|pct`. `scale` multiplies AFTER the None-check (e.g.
    `hit_rate` is a 0..1 fraction — `| pct(1, false, 100)` — while
    `weight_pct` is already percent-scale — `| pct` — without either call
    site needing its own `if x is not none` guard around a multiply)."""
    v = _num_or_none(value)
    if v is None:
        return "—"
    v *= scale
    return f"{v:+.{nd}f}%" if sign else f"{v:.{nd}f}%"


def _fmt_num(value: Any, nd: int = 2, scale: float = 1.0) -> str:
    """Jinja filter `|num` — same None-then-scale discipline as `|pct`."""
    v = _num_or_none(value)
    if v is None:
        return "—"
    return f"{v * scale:.{nd}f}"


def _stat_num(value: Any, suffix: str = "", nd: int = 2) -> str | None:
    """Formats a single ad-hoc market stat figure to a display string, or
    `None` when the value is missing — callers filter `None`s out (a row
    with nothing to show doesn't get a row), used by
    `_volatility_rows`/`_breadth_rows`/`_sentiment_rows` below."""
    v = _num_or_none(value)
    return None if v is None else f"{v:.{nd}f}{suffix}"


def _stat_rows(pairs: list[tuple[str, str | None]]) -> list[dict[str, str]]:
    return [{"label": label, "value": text} for label, text in pairs if text]


def _plural(n: Any, word: str) -> str:
    """Jinja filter `|plural` and a plain Python helper (design review
    P2.13): `_plural(1, "week")` -> `"1 week"`, `_plural(4, "week")` ->
    `"4 weeks"`. Regular English plurals only (every counted noun in this
    template — position/observation/tool/week/symbol — pluralizes with a
    bare `s`); `_plural(None, "week")` -> `"week(s)"`, the honest
    count-unknown glyph the rest of this module already uses."""
    v = _num_or_none(n)
    if v is None:
        return f"{word}(s)"
    count = int(v) if float(v).is_integer() else v
    return f"{count} {word}" if abs(v) == 1 else f"{count} {word}s"


def _week_date_range(report_week: str) -> str | None:
    """`"2026-W33"` -> `"Aug 10 – Aug 16, 2026"` (ISO week Monday..Sunday),
    for the cover's masthead. `None` on anything that isn't a plain
    ISO-week string — the cover simply omits the range rather than guessing."""
    m = re.match(r"^(\d{4})-W(\d{2})$", report_week or "")
    if not m:
        return None
    year, week = int(m.group(1)), int(m.group(2))
    try:
        start = date.fromisocalendar(year, week, 1)
        end = date.fromisocalendar(year, week, 7)
    except ValueError:
        return None
    return f"{start:%b %d} – {end:%b %d, %Y}"


# ── portfolio: positions materiality, allocation, risk, scorecard ─────────
def _split_positions(positions: list[dict]) -> tuple[list[dict], dict[str, Any] | None]:
    """Rule 4: the positions table shows only MATERIAL rows — weight >= 1.0%
    OR the top 12 by weight, whichever set is larger — ranked by weight
    descending; everything else collapses into one tail summary row."""
    ranked = sorted(positions, key=lambda p: _num(p.get("weight_pct")), reverse=True)
    n_material = sum(1 for p in ranked if _num(p.get("weight_pct")) >= _MATERIAL_WEIGHT_PCT)
    n_top = max(n_material, min(_TOP_POSITIONS_MIN_N, len(ranked)))
    top, rest = ranked[:n_top], ranked[n_top:]
    tail = None
    if rest:
        tail = {
            "n": len(rest), "n_label": _plural(len(rest), "position"),
            "value_eur": sum(_num(p.get("value_eur")) for p in rest),
            "weight_pct": sum(_num(p.get("weight_pct")) for p in rest),
        }
    return top, tail


def _allocation_breakdown(positions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rule 4: the donut shows the top 8 positions by value + one "Other"
    slice, with an HTML legend (label, %, swatch color) — canvas text
    rendering isn't worth building for one chart, an HTML legend next to it
    is crisp at any print DPI and needs no `charts.js` change. Each slice
    carries its own `color` (P2.12: "Other" always the fixed neutral grey,
    never a recycled ramp hue) — both the chart-data JSON (canvas) and this
    HTML legend read that same per-slice color, never re-deriving it."""
    ranked = sorted(positions, key=lambda p: _num(p.get("value_eur")), reverse=True)
    total = sum(_num(p.get("value_eur")) for p in ranked)
    head, rest = ranked[:_ALLOCATION_DONUT_MAX_SLICES], ranked[_ALLOCATION_DONUT_MAX_SLICES:]
    slices = [{"label": p.get("symbol") or "—", "value": _num(p.get("value_eur"))} for p in head]
    if rest:
        slices.append({"label": "Other", "value": sum(_num(p.get("value_eur")) for p in rest)})
    for i, s in enumerate(slices):
        s["color"] = _PALETTE_OTHER_HEX if s["label"] == "Other" else _PALETTE_HEX[i % len(_PALETTE_HEX)]
    legend = [
        {"label": s["label"], "pct": (s["value"] / total * 100) if total else 0.0, "color": s["color"]}
        for s in slices
    ]
    return slices, legend


def _coverage_sentence(coverage: dict[str, Any]) -> str | None:
    """Rule 5: one prose sentence built from the risk bundle's coverage
    fields, e.g. "Covers 40 of 50 assets, 90.0% of weight, 300 observations
    from 2025-01-01." — never the raw `coverage` dict."""
    n_covered, n_total = _num_or_none(coverage.get("n_covered")), _num_or_none(coverage.get("n_assets_total"))
    if n_covered is None or n_total is None:
        return None
    sentence = f"Covers {n_covered:.0f} of {n_total:.0f} assets"
    weight_pct = _num_or_none(coverage.get("covered_weight_pct"))
    if weight_pct is not None:
        sentence += f", {weight_pct:.1f}% of weight"
    n_obs = _num_or_none(coverage.get("n_observations"))
    if n_obs is not None:
        sentence += f", {_plural(n_obs, 'observation')}"
    window_start = coverage.get("window_start")
    if window_start:
        sentence += f" from {window_start}"
    return sentence + "."


def _methodology_conventions(portfolio: dict[str, Any]) -> list[str]:
    """Rule 5: the risk/scorecard `coverage.assumption`/`scorecard.convention`
    fields are ALREADY English prose (written by `services/risk.py`'s
    `_ASSUMPTION` / `services/scorecard.py`'s `CONVENTION`) — typeset them as
    paragraphs in the Methodology appendix instead of dict-dumping the
    objects that carry them."""
    lines: list[str] = []
    var_cvar = portfolio.get("var_cvar") or {}
    coverage = var_cvar.get("coverage") if isinstance(var_cvar, dict) else None
    assumption = coverage.get("assumption") if isinstance(coverage, dict) else None
    if isinstance(assumption, str) and assumption:
        lines.append(assumption)
    scorecard = portfolio.get("scorecard") or {}
    convention = scorecard.get("convention") if isinstance(scorecard, dict) else None
    if isinstance(convention, str) and convention:
        lines.append(convention)
    return lines


def _var95_sentence(var_cvar: dict[str, Any]) -> str | None:
    """Rule 3 (feature — per-chapter plain-terms boxes): the VaR95 "everyday"
    sentence, factored out so the Executive Summary panel and the Portfolio
    Review chapter's own panel read IDENTICALLY rather than drifting into two
    slightly different phrasings of the same number. `None` when neither
    `var_eur` is present — never a fabricated figure."""
    var_eur, cvar_eur = var_cvar.get("var_eur"), var_cvar.get("cvar_eur")
    if var_eur is not None and cvar_eur is not None:
        return (
            f"On 19 trading days out of 20, a one-day loss should stay under {_fmt_eur(var_eur)}; "
            f"in the worst 1-in-20 days, expect {_fmt_eur(cvar_eur)} or more."
        )
    if var_eur is not None:
        return f"On 19 trading days out of 20, a one-day loss should stay under {_fmt_eur(var_eur)}."
    return None


def _cash_sentence(nav_eur: Any, cash_eur: Any) -> str | None:
    """P2a: the Executive Summary and Portfolio
    Review boxes used to phrase this two different ways ("Cash is 6.2%" vs
    "Cash, including stablecoins, is 6.2%") for the identical number —
    `report_pack.py` already treats stablecoins as cash upstream, so the
    "including stablecoins" wording is the honest one; both boxes now share
    this one sentence."""
    if not nav_eur or cash_eur is None:
        return None
    return f"Cash, including stablecoins, is {cash_eur / nav_eur * 100:.1f}% of the portfolio."


def _shadow_definition_sentence(chapter_pointer: str | None = None) -> str:
    """One-sentence, plain-language description of what shadow grading is —
    shared verbatim (bar the optional chapter pointer) between the Executive
    Summary roll-up and Chapter 6's own plain-terms box, per the same
    single-source-of-truth reasoning as `_var95_sentence`."""
    pointer = f" ({chapter_pointer})" if chapter_pointer else ""
    return (
        f"Shadow verdicts{pointer} are an evidence-only scoring tool: they grade what each analytical "
        "tool would have said against what actually happened afterward. This is never advice."
    )


def _plain_terms(cover: dict[str, Any], exec_summary: dict[str, Any], portfolio: dict[str, Any], shadow: dict[str, Any]) -> list[str]:
    """Rule 3: the "In plain terms" panel — template-driven sentences
    parameterized ONLY by real values. A missing input drops that sentence
    outright; nothing here is ever invented to fill a gap."""
    lines: list[str] = []

    nav = cover.get("nav_eur")
    if nav is not None:
        lines.append(f"The portfolio is worth {_fmt_eur(nav)}.")

    wow = _num_or_none(cover.get("wow_pct"))
    if wow is not None:
        direction = "up" if wow >= 0 else "down"
        lines.append(f"It moved {direction} {abs(wow):.2f}% from last week.")

    regime_sentence = {
        "risk-on": "Indicators lean risk-on: markets are currently rewarding risk-taking.",
        "risk-off": "Indicators lean risk-off: markets are currently penalizing risk-taking.",
        "neutral": "Indicators are neutral this week. There is no strong lean toward or away from risk-taking.",
    }.get(cover.get("regime"))
    if regime_sentence:
        lines.append(regime_sentence)

    var_cvar = portfolio.get("var_cvar") or {}
    var_sentence = _var95_sentence(var_cvar)
    if var_sentence:
        lines.append(var_sentence)

    fat_tail = _num_or_none(var_cvar.get("fat_tail_ratio"))
    if fat_tail is not None:
        # P2.15: the prior wording ("the fat-tail ratio is
        # 1.60 ... VaR threshold alone suggests") named the METRIC instead of
        # explaining it — not actually plain language. Parameterized by the
        # ratio, no jargon. Fix wave (P1e): period-split, no em dash.
        lines.append(
            f"Extreme bad days hit about {fat_tail:.1f}× harder than typical bad days. The "
            "portfolio's losses have a heavier tail than a normal market would suggest."
        )

    cash_sentence = _cash_sentence(portfolio.get("nav_eur"), portfolio.get("cash_eur"))
    if cash_sentence:
        lines.append(cash_sentence)

    if shadow and not shadow.get("absent"):
        lines.append(_shadow_definition_sentence("Chapter 6"))

    return lines


# ── per-chapter "In plain terms" boxes (feature, extends rule 3) ──────────
# Same discipline as `_plain_terms` above (the Executive Summary's box): every
# sentence is gated on a real value from THIS chapter's own data — a missing
# input drops its sentence outright, nothing is ever invented to fill a gap.
# Each box is placed at the END of its chapter (rendered after the chapter's
# data, in `report_pack.html`, via the shared `plain_terms_box` macro), so it
# reads as interpretation of what was just shown rather than a preview of it.
# P1a: the "concentration is the dominant risk
# factor" framing was printing unconditionally for ANY largest holding, even
# a mild one in a genuinely diversified book — gated on a real concentration
# threshold now; below it, only the factual weight is stated.
_PORTFOLIO_CONCENTRATION_THRESHOLD_PCT = 25.0
# P2c: match the 4-week grading floor the shadow
# layer itself uses (`shadow.grade_min_weeks`) before the scorecard box will
# characterize active management as "better"/"worse" than doing nothing —
# the old 2-week floor let the box render a verdict sooner than the shadow
# chapter would trust its own tools to. Falls back to this constant only
# when the shadow chapter's own value isn't available.
_PORTFOLIO_SCORECARD_MIN_WEEKS_DEFAULT = 4


def _plain_terms_portfolio(portfolio: dict[str, Any], grade_min_weeks: Any = None) -> list[str]:
    lines: list[str] = []
    if not portfolio or portfolio.get("absent"):
        return lines

    positions = portfolio.get("positions") or []
    if positions:
        largest = max(positions, key=lambda p: _num(p.get("weight_pct")))
        symbol, weight = largest.get("symbol"), _num_or_none(largest.get("weight_pct"))
        if symbol and weight is not None:
            if weight >= _PORTFOLIO_CONCENTRATION_THRESHOLD_PCT:
                lines.append(
                    f"{symbol} alone is {weight:.1f}% of the portfolio. Concentration is the dominant "
                    "risk factor here."
                )
            else:
                lines.append(f"{symbol} is the largest holding at {weight:.1f}%.")

    cash_sentence = _cash_sentence(portfolio.get("nav_eur"), portfolio.get("cash_eur"))
    if cash_sentence:
        lines.append(cash_sentence)

    var_sentence = _var95_sentence(portfolio.get("var_cvar") or {})
    if var_sentence:
        lines.append(var_sentence)

    scorecard = portfolio.get("scorecard") or {}
    depth_weeks = _num_or_none(scorecard.get("depth_weeks"))
    min_weeks = _num_or_none(grade_min_weeks)
    if min_weeks is None:
        min_weeks = _PORTFOLIO_SCORECARD_MIN_WEEKS_DEFAULT
    if scorecard.get("empty") or depth_weeks is None or depth_weeks < min_weeks:
        lines.append(
            "There isn't enough weekly history yet to judge whether active decisions are helping or "
            "hurting returns."
        )
    else:
        actual, do_nothing = _num_or_none(scorecard.get("actual_twr")), _num_or_none(scorecard.get("do_nothing_twr"))
        if actual is not None and do_nothing is not None:
            diff = actual - do_nothing
            if abs(diff) < 0.0005:
                lines.append("So far, actively managing the portfolio has performed about the same as simply leaving it alone.")
            else:
                verb = "done better than" if diff > 0 else "done worse than"
                lines.append(f"So far, actively managing the portfolio has {verb} simply leaving it alone.")

    return lines


_MARKET_REGIME_PLAIN_SENTENCES = {
    "risk-on": "Most market indicators favour risk-taking this week.",
    "risk-off": "Most market indicators favour caution this week.",
    "neutral": "Market indicators are mixed this week. There is no strong lean either way.",
}


def _plain_terms_market(market: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if not market or market.get("absent"):
        return lines

    regime = market.get("regime") if isinstance(market.get("regime"), dict) else {}
    sentence = _MARKET_REGIME_PLAIN_SENTENCES.get(regime.get("state"))
    if sentence:
        lines.append(sentence)

    cross_asset = market.get("cross_asset") if isinstance(market.get("cross_asset"), dict) else {}
    snapshot = cross_asset.get("snapshot") if isinstance(cross_asset.get("snapshot"), list) else []
    movers = [
        (s, _num_or_none(s.get("chg_1w_pct"))) for s in snapshot if isinstance(s, dict)
    ]
    movers = [(s, v) for s, v in movers if v is not None]
    if movers:
        biggest, value = max(movers, key=lambda pair: abs(pair[1]))
        label = biggest.get("label") or biggest.get("symbol") or "—"
        lines.append(f"{label} moved {value:+.1f}% this week. That was the largest move across major assets.")

    valuation = market.get("valuation") if isinstance(market.get("valuation"), dict) else {}
    metrics = valuation.get("metrics") if isinstance(valuation.get("metrics"), list) else []
    cape = next((m for m in metrics if isinstance(m, dict) and m.get("key") == "cape"), None)
    if isinstance(cape, dict) and not cape.get("needs_source"):
        pctile = _num_or_none(cape.get("percentile"))
        if pctile is not None:
            lines.append(f"US equities are priced richer than ~{pctile:.0f}% of history on the Shiller measure.")

    return lines


_ALTMAN_DISTRESS_VERDICT = "Distress"
_BENEISH_ELEVATED_VERDICT = "Elevated manipulation risk"


def _plain_terms_holdings(holdings: dict[str, Any], material_symbols: set[str] | None = None) -> list[str]:
    """The three forensic-score definition sentences are unconditional
    (explaining what a score IS is not a value derived from this week's
    data), everything after them is value-gated on this week's real cards —
    see the module-docstring rule this section implements.

    P1c: every other sentence here is now restricted
    to `material_symbols` (the SAME set that gates a symbol into a printed
    tear-sheet card, `_holdings_view`'s `hv.material`) — the box must never
    name a figure the reader can't find printed anywhere else in the
    chapter. A non-material name (e.g. a <1%-weight roster row) never gets
    cited here even if it happens to carry a real score."""
    lines: list[str] = []
    if not holdings or holdings.get("absent"):
        return lines

    lines.extend([
        "Piotroski's F-Score (0–9) is a rough read on business quality. It covers profitability, cash flow, and balance-sheet trend.",
        "Altman's Z-Score estimates a company's distance from bankruptcy.",
        "Beneish's M-Score flags accounting patterns associated with earnings manipulation.",
    ])

    symbols = holdings.get("symbols") if isinstance(holdings.get("symbols"), dict) else {}
    material_symbols = material_symbols or set()
    real_entries = {
        s: e for s, e in symbols.items()
        if s in material_symbols and isinstance(e, dict) and not e.get("absent")
    }

    def card(entry: dict[str, Any], key: str) -> dict[str, Any] | None:
        cards = ((entry.get("forensic") or {}).get("cards")) or []
        return next((c for c in cards if isinstance(c, dict) and c.get("key") == key), None)

    piotroski_scores: list[tuple[str, float]] = []
    distress_names: list[str] = []
    manipulation_names: list[str] = []
    any_real_beneish = False
    any_crypto = False
    for symbol, entry in real_entries.items():
        if _is_crypto(entry, symbol):
            any_crypto = True
            continue
        p = card(entry, "piotroski")
        score = _num_or_none(p.get("score")) if isinstance(p, dict) else None
        if score is not None:
            piotroski_scores.append((symbol, score))
        z = card(entry, "altman")
        if isinstance(z, dict) and z.get("verdict") == _ALTMAN_DISTRESS_VERDICT:
            distress_names.append(symbol)
        m = card(entry, "beneish")
        if isinstance(m, dict) and m.get("score") is not None:
            any_real_beneish = True
            if m.get("verdict") == _BENEISH_ELEVATED_VERDICT:
                manipulation_names.append(symbol)

    if len(piotroski_scores) == 1:
        symbol, score = piotroski_scores[0]
        lines.append(f"{symbol} is the only material holding with a Piotroski score this week ({score:.0f}/9).")
    elif len(piotroski_scores) >= 2:
        strongest = max(piotroski_scores, key=lambda pair: pair[1])
        weakest = min(piotroski_scores, key=lambda pair: pair[1])
        if strongest[0] != weakest[0]:
            lines.append(
                f"{strongest[0]} has the strongest Piotroski score this week ({strongest[1]:.0f}/9). "
                f"{weakest[0]} has the weakest ({weakest[1]:.0f}/9)."
            )

    if distress_names:
        verb = "sits" if len(distress_names) == 1 else "sit"
        lines.append(f"{', '.join(distress_names)} {verb} in Altman's distress zone this week.")

    if any_real_beneish:
        if manipulation_names:
            verb = "flags" if len(manipulation_names) == 1 else "flag"
            lines.append(f"{', '.join(manipulation_names)} {verb} elevated earnings-manipulation risk on the Beneish measure.")
        else:
            lines.append("No manipulation flags across covered names on the Beneish measure.")

    if any_crypto:
        lines.append(
            "Crypto holdings don't get forensic accounting scores. Those scores need reported "
            "financial statements, which crypto assets don't file. That's expected, not missing data."
        )

    return lines


_QUANT_IC_NO_EDGE_THRESHOLD = 0.05


def _plain_terms_quant(quant: dict[str, Any]) -> list[str]:
    """P2b: the small-universe/"within-book"
    caveat is dropped from this box — the existing how-to-read caption
    printed directly above the factor summary already states it
    ("Cross-section universe: N names ... within-book ordering, not an
    independent market factor reading"), so repeating it here read as the
    same claim stated twice on one page."""
    lines: list[str] = []
    if not quant or quant.get("absent"):
        return lines
    factor = quant.get("factor") if isinstance(quant.get("factor"), dict) else {}
    if not factor or factor.get("absent"):
        return lines

    lines.append(
        "A factor IC (information coefficient) tests whether ranking names by a signal, such as "
        "momentum. It checks whether that ranking predicted what actually happened next. An IC near "
        "zero means no predictive edge was found."
    )

    ic_mean = _num_or_none(factor.get("ic_mean"))
    if ic_mean is not None:
        if abs(ic_mean) < _QUANT_IC_NO_EDGE_THRESHOLD:
            verdict = "effectively no edge in this small universe"
        else:
            verdict = f"a weak {'positive' if ic_mean > 0 else 'negative'} signal in this small universe"
        factor_name = factor.get("factor")
        label = f"This week's {factor_name} IC" if factor_name else "This week's IC"
        lines.append(f"{label} was {ic_mean:+.2f}, {verdict}.")

    return lines


def _shadow_grading_status_sentence(shadow: dict[str, Any]) -> str | None:
    """P1b: the box used to say "N tools graded
    this week" flatly, while the scoreboard printed directly above it shows
    the SAME tools' `N graded` column at 0 and `Hit rate` as "ungraded" —
    `graded` on a tool means "this attempt was scored", not "produced a real
    hit rate", and the box must not blur that distinction the table already
    makes honest. Reuses `_shadow_rollup`'s own attempted-vs-hit-rate split
    (Executive Summary roll-up), phrased for the box: periods instead of an
    em dash, and no "see Chapter 6" pointer since this sentence IS Chapter 6."""
    if not shadow or shadow.get("absent"):
        return None
    tools = shadow.get("tools") if isinstance(shadow.get("tools"), dict) else {}
    graded = [(name, t) for name, t in tools.items() if isinstance(t, dict) and t.get("graded")]
    if not graded:
        return "No tools have been graded yet this week."
    scored = [(name, _num_or_none(t.get("hit_rate"))) for name, t in graded]
    with_hit_rate = [(name, hr) for name, hr in scored if hr is not None]
    if not with_hit_rate:
        return (
            f"{_plural(len(graded), 'tool')} were graded this attempt. None had enough events yet "
            "for a real hit rate."
        )
    parts = [f"{name} {hr * 100:.0f}%" for name, hr in with_hit_rate]
    return f"{', '.join(parts)} had a real hit rate this week."


def _plain_terms_shadow(shadow: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if not shadow or shadow.get("absent"):
        return lines

    lines.append(_shadow_definition_sentence())

    status_sentence = _shadow_grading_status_sentence(shadow)
    if status_sentence:
        lines.append(status_sentence)

    tools = shadow.get("tools") if isinstance(shadow.get("tools"), dict) else {}
    graded = [t for t in tools.values() if isinstance(t, dict) and t.get("graded")]
    if graded and all(t.get("low_confidence") for t in graded):
        lines.append(
            "None of the tools has enough graded history yet for a track record. Treat all tool "
            "opinions as unproven."
        )

    return lines


def _shadow_rollup(shadow: dict[str, Any]) -> str | None:
    """One-line shadow-grading roll-up for the Executive Summary (rule 2) —
    full detail stays in Chapter 6; this is a pointer, not a duplicate table."""
    if not shadow or shadow.get("absent"):
        return None
    tools = shadow.get("tools") if isinstance(shadow.get("tools"), dict) else {}
    graded = [(name, t) for name, t in tools.items() if isinstance(t, dict) and t.get("graded")]
    scored = [(name, _num_or_none(t.get("hit_rate"))) for name, t in graded]
    with_hit_rate = [(name, hr) for name, hr in scored if hr is not None]
    if not with_hit_rate:
        # Every graded tool still came back with no hit rate this week (too
        # few events to score, not the same as "never graded") — naming each
        # one "ungraded" in a row reads as noise, not signal.
        n = len(scored)
        return (
            f"Shadow grading: {_plural(n, 'tool')} graded this attempt, none with enough events yet "
            "for a hit rate — see Chapter 6." if n else
            "Shadow grading: no tool has enough graded history yet this week — see Chapter 6."
        )
    parts = [f"{name} {hr * 100:.0f}%" for name, hr in with_hit_rate]
    return f"Shadow grading roll-up — {', '.join(parts)} hit rate this week (evidence-only — see Chapter 6)."


# ── forecast ledger + calibration chapters ─────────────────
# F6a (fix round): fields that are CONSTANT (`brier_coin` — always 0.25,
# never derived from `n`) or purely IMPLIED BY `n == 0` (`effective_n` is a
# deterministic function of `n`, always 0 right along with it) — dropped
# outright once `summary.n == 0`, regardless of their own formatted value,
# because they carry no information beyond what "N (decided) = 0" already
# says. `pushes`/`abstained` are NOT in this set: they are independent
# counts (a group can have decided N == 0 while still carrying real pushes/
# abstentions) — those two are dropped only when THEIR OWN value is also
# exactly 0, never when non-zero (a real count is never "no value yet").
_CALIBRATION_ZERO_N_ALWAYS_DROP = ("effective_n", "brier_coin")
_CALIBRATION_ZERO_N_DROP_IF_ZERO = ("pushes", "abstained")
# all four Brier-family stats used
# mixed precision (`num3` for mean/ref, `num2` for coin/skill) — a reader
# comparing `brier_mean` against `brier_skill` saw a different number of
# decimals for numbers of the same kind. One convention throughout: `num3`
# (the more informative of the two — Brier deltas this small legitimately
# need the third digit; unifying downward to `num2` would have hidden real
# differences instead).
_CALIBRATION_STAT_LABELS: tuple[tuple[str, str, str], ...] = (
    # (summary field, display label, formatter — "num"/"pct"/"num0")
    ("n", "N (decided)", "num0"),
    ("effective_n", "Effective N", "num0"),
    ("brier_mean", "Brier (mean)", "num3"),
    ("brier_ref", "Brier ref (group base rate)", "num3"),
    ("brier_coin", "Brier (0.25 coin, reference only)", "num3"),
    ("brier_skill", "Brier skill", "num3"),
    ("hit_rate", "Hit rate", "pct"),
    ("benchmark_hit_rate", "Benchmark hit rate", "pct"),
    ("wilson_lb", "Wilson LB", "num2"),
    ("pushes", "Pushes", "num0"),
    ("abstained", "Abstained", "num0"),
    ("abstention_rate", "Abstention rate", "pct"),
)


def _display_issuer(issued_by: str) -> str:
    """`"shadow:technicals"` -> `"Technicals"`, `"human"` -> `"Human"` — the calibration chapter's
    group heading must never show the raw `issued_by` key verbatim; strip
    the `shadow:` namespace prefix machine issuers carry internally (same
    split `shadow.py`/`forecast_resolver.py` already use for their own
    tool-name logic) and title-case whatever remains."""
    if not isinstance(issued_by, str) or not issued_by:
        return "—"
    name = issued_by.split(":", 1)[1] if issued_by.startswith("shadow:") else issued_by
    return name.replace("_", " ").replace("-", " ").title()


def _calibration_groups_view(groups: list[Any]) -> list[dict[str, Any]]:
    """Chapter-body view for the Calibration chapter: one block per
    `issued_by` x `horizon_days` group — a summary stat-row list (reusing
    the existing `.stat-row`/`.stat-group` partial, same pattern as the
    Market chapter's volatility/breadth/sentiment blocks) plus a reliability
    table only for groups whose `bins` is present (B2 — `None` means the
    issuer's probability is a near-constant track-record prior, and
    `bins_note` explains why instead). Tolerant of a malformed/missing
    `summary`/`bins`/`groups` entry — a group with nothing real to show
    still renders its identity + pending count, never raises (mirrors the
    acceptance line: an empty `outcomes` table must still render every
    pending-only group with `summary.n == 0`)."""
    out: list[dict[str, Any]] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        summary = g.get("summary") if isinstance(g.get("summary"), dict) else {}
        all_rows = [
            (field, label, _fmt_pct(summary.get(field), 1, False, 100) if kind == "pct" else
             _fmt_num(summary.get(field), 0) if kind == "num0" else
             _fmt_num(summary.get(field), 3) if kind == "num3" else
             _fmt_num(summary.get(field), 2))
            for field, label, kind in _CALIBRATION_STAT_LABELS
        ]
        # Fix-first #3: a group with nothing graded yet
        # (fresh issuer, summary.n == 0) rendered 9-of-12 stat rows as a bare
        # "—" — real estate spent showing nothing. Drop the no-value rows and
        # note the omission once, instead of once per row. F6a (fix round):
        # `summary.n == 0` ALSO drops the constant/implied rows that render a
        # real (non-"—") but uninformative value — see
        # `_CALIBRATION_ZERO_N_ALWAYS_DROP`/`_CALIBRATION_ZERO_N_DROP_IF_ZERO`
        # above — leaving "N (decided)" as the single surviving row in the
        # common (nothing has happened yet) case.
        zero_n = summary.get("n") == 0
        present_rows = []
        omitted_labels: list[str] = []
        for field, label, text in all_rows:
            if text == "—":
                omitted_labels.append(label)
                continue
            if zero_n and field in _CALIBRATION_ZERO_N_ALWAYS_DROP:
                omitted_labels.append(label)
                continue
            if zero_n and field in _CALIBRATION_ZERO_N_DROP_IF_ZERO and (summary.get(field) or 0) == 0:
                omitted_labels.append(label)
                continue
            present_rows.append((label, text))
        # the omission footnote
        # used to print only a bare count ("9 stat(s) omitted — no value
        # yet.") — a reader couldn't tell WHICH nine without cross-checking
        # `_CALIBRATION_STAT_LABELS` themselves. `stats_omitted` is now the
        # list of omitted labels, not a count — the template names them.
        stats_omitted = omitted_labels
        stat_rows = _stat_rows(present_rows)
        bins = g.get("bins")
        bin_rows = None
        if isinstance(bins, list) and bins:
            bin_rows = []
            for b in bins:
                n_raw = _num_or_none(b.get("n")) if isinstance(b, dict) else None
                bin_rows.append({
                    "bin": b.get("bin", "—") if isinstance(b, dict) else "—",
                    "n": _fmt_num(b.get("n"), 0) if isinstance(b, dict) else "0",
                    # F6b (fix round): `n_raw is None` (malformed/missing —
                    # `_reliability_bins` itself always emits a real int)
                    # must never be marked "(n=0)" — that badge asserts a
                    # KNOWN-zero bin, not an unknown one.
                    "empty": n_raw == 0,
                    "mean_p": _fmt_num(b.get("mean_p"), 2) if isinstance(b, dict) else "—",
                    "hit_rate": _fmt_pct(b.get("hit_rate"), 1, False, 100) if isinstance(b, dict) else "—",
                    "wilson_lb": _fmt_num(b.get("wilson_lb"), 2) if isinstance(b, dict) else "—",
                })
        out.append({
            "issued_by": g.get("issued_by", "—"),
            "issuer_label": _display_issuer(g.get("issued_by")),
            "horizon_days": g.get("horizon_days", "—"),
            "stat_rows": stat_rows,
            "stats_omitted": stats_omitted,
            "bins": bin_rows,
            "bins_note": g.get("bins_note"),
            "pending": g.get("pending", 0),
            # `priced_as_of` was already on every group (same ledger
            # shape the shadow table-level caption reads) but silently
            # dropped here — never reached the template.
            "priced_as_of": g.get("priced_as_of"),
            "low_confidence": bool(g.get("low_confidence")),
            "deflation_note": g.get("deflation_note"),
        })
    return out


def _plain_terms_calibration(calibration: dict[str, Any]) -> list[str]:
    """Value-gated "In plain terms" sentences (same discipline as
    `_plain_terms_shadow`): what Brier/Brier-skill mean and that the
    reference is the group's OWN base rate (never a coin), why machine
    issuers show discrimination instead of reliability bins, and that early
    N=0 rows are expected, not a bug."""
    lines: list[str] = []
    if not calibration or calibration.get("absent"):
        return lines
    groups = calibration.get("groups") if isinstance(calibration.get("groups"), list) else []
    if not groups:
        return lines

    lines.append(
        "The Brier score measures how well an issuer's stated probabilities matched what "
        "actually happened; lower is better. The reference (\"Brier skill\") is that issuer's "
        "own realised base rate, not a coin flip. The 0.25 coin figure is shown only for "
        "context."
    )

    has_machine = any(
        isinstance(g, dict) and isinstance(g.get("issued_by"), str) and g["issued_by"].startswith("shadow:")
        for g in groups
    )
    if has_machine:
        lines.append(
            "Machine-issued forecasts carry a near-constant probability drawn from their own "
            "track record. They show discrimination (hit rate versus benchmark hit rate) "
            "instead of a reliability table, which would just repeat one number."
        )

    real_groups = [g for g in groups if isinstance(g, dict)]
    if real_groups and all(
        isinstance(g.get("summary"), dict) and (g["summary"].get("n") or 0) == 0 for g in real_groups
    ):
        lines.append(
            "Every group shows zero graded forecasts so far. That is expected in the first "
            "weeks after issuance. A forecast only counts here once its outcome resolves."
        )

    return lines


def _plain_terms_forecasts(forecasts: dict[str, Any]) -> list[str]:
    """The Forecasts chapter was
    the one analytical chapter with no "In plain terms" box (Methodology
    gets none by design — it IS the explainer; see the Feature note above).
    Same value-gated-sentence discipline as every other box: reads ONLY
    this chapter's own already-normalized fields, drops a sentence outright
    when its input is missing, never invents one."""
    lines: list[str] = []
    if not forecasts or forecasts.get("absent"):
        return lines

    issued = forecasts.get("issued")
    if issued is not None:
        lines.append(
            f"{int(issued)} new forecast(s) were issued this week — each one a specific, "
            "checkable claim (e.g. \"AAA closes higher in 28 days\") graded once its due date "
            "arrives and its outcome resolves, feeding the Calibration chapter."
        )
    pending_total = forecasts.get("pending_total")
    if pending_total is not None:
        lines.append(
            f"{int(pending_total)} forecast(s) across all issuers are still open, awaiting "
            "their due date before they can be graded."
        )
    duplicates = forecasts.get("duplicates")
    if duplicates:
        lines.append(
            f"{int(duplicates)} candidate forecast(s) this week duplicated an already-open call "
            "for the same subject and were skipped, not double-counted."
        )
    next_due = forecasts.get("next_due")
    if next_due:
        lines.append(f"The next forecast due for grading is {next_due}.")
    return lines


# ── holdings: materiality split (tear sheets vs. roster) ──────────────────
# P1d: real packs render `fundamentals.sector` as
# missing (`None`) for coins — `research.py`'s `FundamentalsAdapter` is
# yfinance-backed and yfinance has no sector for BTC/ETH, so it never gets
# the chance to set `sector="Crypto"` the way it does for an equity. The
# known-ticker set below REUSES `services/markets.py`'s `_CRYPTO` list (the
# one place this codebase already enumerates "coins we track"), rather than
# inventing a second one here.
_KNOWN_CRYPTO_TICKERS = frozenset(ticker for _coingecko_id, ticker in _MARKETS_CRYPTO)


def _is_crypto(entry: dict[str, Any], symbol: Any = None) -> bool:
    """A real (non-empty) sector on file is authoritative either way — trust
    it before falling back to anything else. Only when sector is BOTH
    missing AND there is no forensic score of any kind on file (a real
    equity with a temporarily-missing sector could still have real scores;
    a coin never will) does the symbol get matched against the codebase's
    known-crypto ticker set."""
    sector = ((entry.get("fundamentals") or {}).get("sector") or "")
    if isinstance(sector, str) and sector.strip().lower() == "crypto":
        return True
    if isinstance(sector, str) and sector.strip():
        return False
    cards = ((entry.get("forensic") or {}).get("cards")) or []
    has_any_score = any(isinstance(c, dict) and c.get("score") is not None for c in cards)
    if has_any_score:
        return False
    return isinstance(symbol, str) and symbol.upper() in _KNOWN_CRYPTO_TICKERS


def _forensic_all_blank(cards: list[Any]) -> bool:
    return bool(cards) and all(
        isinstance(c, dict) and c.get("verdict") is None and c.get("score") is None for c in cards
    )


def _absent_symbol_label(symbol: Any, weight_pct: Any) -> str:
    """`"XYZ (5.0%)"` — weight omitted (`"XYZ"` alone) when unknown."""
    w = _fmt_pct(weight_pct)
    return f"{symbol} ({w})" if w != "—" else str(symbol)


def _holdings_view(holdings: dict[str, Any], positions: list[dict], material_symbols: set[str]) -> dict[str, Any]:
    """Rule 8: material positions (same threshold as rule 4) get a tear-sheet
    card; everything else collapses into one compact roster table (symbol,
    value, weight, trend — no card). Cards/roster follow the SAME
    weight-descending order as the positions table, not dict insertion
    order, so the two chapters read as one consistent ranking.

    P0: a MATERIAL position with no
    `holdings.symbols[symbol]` entry AT ALL (not even a per-symbol
    `{"absent": True, ...}` placeholder — this chapter's builder never
    attempted it, or the two source lists genuinely diverged for the week)
    used to fall out of BOTH the `symbol not in symbols_map` guard below and
    the roster's `entry` type-check — silently dropped from the whole
    chapter. The chapter's own how-to-read caption promises a tear sheet or
    an absent reason for every held symbol. A material position could vanish
    from the chapter while a zero-value dust card still rendered.

    That fix's own rendering was the next problem — every no-data/absent
    material symbol got its own full tear-sheet card repeating one sentence
    ("Tear sheet unavailable — ...") verbatim, when many material
    symbols have no data. Absent
    symbols no longer get individual cards at all: they're grouped here by
    DISTINCT reason text (`absent_groups`, ordered by first occurrence,
    weight-descending same as everything else) — the synthetic
    "no data recorded" cohort collapses to one sentence naming every symbol
    + weight in it, and a genuine per-symbol reason (e.g. "delisted ticker")
    gets its own group/line, never merged with an unrelated reason. Nothing
    is lost: every material symbol + its weight + its reason is still
    findable, just not as a run of near-identical paragraphs. `material` now
    contains ONLY real (non-absent) entries — the template's card loop no
    longer branches on `h.get("absent")` at all."""
    symbols_map = holdings.get("symbols") if isinstance(holdings.get("symbols"), dict) else {}
    pos_by_symbol = {p.get("symbol"): p for p in positions}
    ranked_symbols = [p.get("symbol") for p in sorted(positions, key=lambda p: _num(p.get("weight_pct")), reverse=True)]
    ordered = ranked_symbols + [s for s in symbols_map if s not in pos_by_symbol]

    material: list[dict[str, Any]] = []
    roster: list[dict[str, Any]] = []
    absent_by_reason: dict[str, list[str]] = {}
    reason_order: list[str] = []
    seen: set[Any] = set()
    for symbol in ordered:
        if symbol in seen:
            continue
        seen.add(symbol)
        entry = symbols_map.get(symbol)
        if symbol in material_symbols:
            if not isinstance(entry, dict) or entry.get("absent"):
                reason = (
                    "no data recorded this week" if not isinstance(entry, dict)
                    else (entry.get("reason") or "no data")
                )
                weight_pct = (pos_by_symbol.get(symbol) or {}).get("weight_pct")
                if reason not in absent_by_reason:
                    absent_by_reason[reason] = []
                    reason_order.append(reason)
                absent_by_reason[reason].append(_absent_symbol_label(symbol, weight_pct))
                continue
            cards = ((entry.get("forensic") or {}).get("cards")) or []
            other_cards = [c for c in cards if not (isinstance(c, dict) and c.get("key") == "piotroski")]
            material.append({
                "symbol": symbol, "entry": entry,
                "is_crypto": _is_crypto(entry, symbol),
                "forensic_blank": _forensic_all_blank(other_cards),
                "other_forensic_cards": other_cards,
            })
        elif isinstance(entry, dict):
            pos = pos_by_symbol.get(symbol) or {}
            roster.append({
                "symbol": symbol,
                "value_eur": pos.get("value_eur"), "weight_pct": pos.get("weight_pct"),
                "trend": ((entry.get("technicals") or {}).get("trend")) if not entry.get("absent") else None,
            })
        # else: non-material AND no tear-sheet entry at all — the positions
        # table's tail row already carries this symbol's aggregate value/
        # weight; the roster makes no per-row promise the way the chapter
        # caption does for material cards, so there is nothing dishonest
        # about it having no row of its own here.
    absent_groups = [
        {"reason": reason, "symbols_label": ", ".join(absent_by_reason[reason])}
        for reason in reason_order
    ]
    return {"material": material, "roster": roster, "absent_groups": absent_groups}


# ── market: bespoke typeset readers (rule 7 — no generic key/value dump) ──
# P1.9: a prior pass added a "N further field(s) recorded"
# residual note for market fields with no bespoke reader (rotation/rrg,
# outside the redesign brief's stated scope). Sitting directly under the
# calendar table with no visual separation, it reads as if it's ABOUT the
# calendar (rows hidden) rather than about unrelated rotation/rrg data —
# confusing, not honest. Dropped; rotation/rrg were never
# in-scope for this pack per the original spec (rule 7 names regime/
# cross-asset/vol/breadth/sentiment/valuation/econ-calendar explicitly, not
# rotation/rrg), so there is no promise left unmet by their absence — unlike
# the P0 holdings-materiality gate, where the chapter caption DOES promise
# coverage and silently dropping it would be dishonest.


def _correlation_pairs(cross_asset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Rule 7: never print the correlation matrix — the highest/lowest
    correlated pairs (top 3 / bottom 3), computed from the existing
    `matrix`+`symbols` fields."""
    symbols = cross_asset.get("symbols")
    matrix = cross_asset.get("matrix")
    if not isinstance(symbols, list) or not isinstance(matrix, list):
        return {"top": [], "bottom": []}
    labels = {
        s.get("symbol"): s.get("label") for s in (cross_asset.get("snapshot") or []) if isinstance(s, dict)
    }
    pairs: list[dict[str, Any]] = []
    n = len(symbols)
    for i in range(n):
        row = matrix[i] if i < len(matrix) and isinstance(matrix[i], list) else []
        for j in range(i + 1, n):
            value = _num_or_none(row[j]) if j < len(row) else None
            if value is None:
                continue
            a, b = symbols[i], symbols[j]
            pairs.append({"pair": f"{labels.get(a, a)} / {labels.get(b, b)}", "value": value})
    top = sorted(pairs, key=lambda p: p["value"], reverse=True)[:3]
    bottom = sorted(pairs, key=lambda p: p["value"])[:3]
    return {"top": top, "bottom": bottom}


# P1.9: rank calendar events by a deterministic keyword
# importance tier instead of upstream feed order — tier 1 is the handful of
# releases that actually move markets, tier 2 a wider "worth knowing" band,
# everything else tier 3 (dropped from the printed table entirely; the raw
# `sections` data is untouched, this is a display-only cut). Matched
# case-insensitively as a substring of the event name.
_CALENDAR_TIER1_KEYWORDS = ("fomc", "cpi", "hicp", "inflation", "payroll", "employment", "gdp")
_CALENDAR_TIER2_KEYWORDS = ("pmi", "ism", "retail sales", "confidence", "fed speaker", "fed chair")
_CALENDAR_MAX_SHOWN_ROWS = 8

# Fix round 7: a trailing dash-separated qualifier —
# "Employment Situation - Texas", "State Employment and Unemployment -
# California", ... — is how BLS state-level releases repeat one national
# headline once per state; before this the exact-string dedupe below let 4+
# near-identical rows fill the printed table's fixed row budget (rows 5-8 of
# an 8-row cap, in the real proof render). Stripped BEFORE dedupe/tier/cap
# run, not after — a state variant must never occupy a cap slot a genuinely
# distinct release could have used.
_CALENDAR_REGION_SUFFIX_RE = re.compile(r"\s*[-–—]\s*[A-Za-z][A-Za-z .]*$")


def _normalize_calendar_event_title(name: str) -> str:
    stripped = _CALENDAR_REGION_SUFFIX_RE.sub("", name).strip()
    return stripped or name


def _calendar_tier(event_name: str) -> int:
    name = event_name.lower()
    if any(k in name for k in _CALENDAR_TIER1_KEYWORDS):
        return 1
    if any(k in name for k in _CALENDAR_TIER2_KEYWORDS):
        return 2
    return 3


def _econ_calendar_rows(market: dict[str, Any]) -> dict[str, Any]:
    """Rule 7 + P1.9: distinct named events only (deduped by event name),
    ranked by `_calendar_tier` (date within a tier) and capped at
    `_CALENDAR_MAX_SHOWN_ROWS` tier-1/2 rows — tier-3 "noise" (H.10 FX
    Rates, ICE BofA Indices, ...) never makes the printed table at all,
    unlike the prior feed-order cap which just showed whatever the source
    listed first. The actual/previous/consensus columns are dropped
    ENTIRELY (not shown as an all-em-dash wall) when every shown row has all
    three empty — the common case for the FRED-only calendar adapter, which
    publishes names/dates but no print values."""
    envelope = market.get("econ_calendar_week_ahead")
    if not isinstance(envelope, dict):
        return {"rows": [], "show_actual_cols": False, "stale_note": None}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    events = data.get("events") if isinstance(data.get("events"), list) else []
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        raw_name = e.get("event")
        if not raw_name:
            continue
        # P1.9's dedupe (before this fix) matched on the RAW name only — a
        # state-qualified variant ("... - Texas") never collided with its
        # national counterpart. Normalize first so state/region variants of
        # the same release collapse into one row before tier/cap ever run.
        name = _normalize_calendar_event_title(str(raw_name))
        if name in seen:
            continue
        seen.add(name)
        deduped.append({
            "date": e.get("date") or "", "event": name, "tier": _calendar_tier(name),
            "actual": e.get("actual"), "previous": e.get("previous"), "consensus": e.get("consensus"),
        })
    ranked = sorted(deduped, key=lambda r: (r["tier"], r["date"]))
    rows = [r for r in ranked if r["tier"] < 3][:_CALENDAR_MAX_SHOWN_ROWS]
    show_actual_cols = any(r["actual"] or r["previous"] or r["consensus"] for r in rows)
    stale_note = None
    if envelope.get("stale"):
        stale_note = "This calendar is served from the last good fetch — the live source was unavailable this run."
    elif data.get("needs_key"):
        stale_note = "Economic calendar needs a FRED API key configured — event names/dates only otherwise."
    return {"rows": rows, "show_actual_cols": show_actual_cols, "stale_note": stale_note}


def _regime_components_view(regime: dict[str, Any]) -> dict[str, Any]:
    """Rule 7: components with no score this week (e.g. a data source that
    hasn't warmed up) drop OUT of the table into one muted footnote line
    ("Not available this week: Volatility, Credit") — never a row of em-dash
    score/contribution cells sitting next to real ones.

    P1.10: when components ARE missing, the shown
    contributions no longer sum to the headline score (`market.regime.score`
    is renormalized upstream over only the available components) — a reader
    doing the arithmetic on the printed rows would conclude the table is
    wrong. State the renormalization inline, on the same footnote line, only
    when it actually applies (nothing to renormalize if every component is
    present)."""
    components = regime.get("components") if isinstance(regime.get("components"), list) else []
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for c in components:
        if not isinstance(c, dict):
            continue
        label = c.get("label") or c.get("key") or "—"
        score = _num_or_none(c.get("score"))
        if score is None:
            missing.append(str(label))
            continue
        rows.append({
            "label": label, "score": score,
            "weight": _num_or_none(c.get("weight")), "contribution": _num_or_none(c.get("contribution")),
        })
    missing_note = None
    if missing:
        missing_note = (
            f"Not available this week: {', '.join(missing)}. "
            "Score is renormalized over the available components, so the printed "
            "contributions above will not sum to the headline score."
        )
    return {"rows": rows, "missing": missing, "missing_note": missing_note}


def _volatility_rows(vol: Any) -> list[dict[str, str]]:
    if not isinstance(vol, dict):
        return []
    return _stat_rows([
        ("VIX", _stat_num(vol.get("vix"))),
        ("VIX percentile", _stat_num(vol.get("vix_percentile"), "th pct", 1)),
        ("VIX3M", _stat_num(vol.get("vix3m"))),
        ("Term structure", str(vol["term_state"]) if vol.get("term_state") else None),
        ("Realized vol (21d)", _stat_num(vol.get("realized_vol_pct"), "%")),
        ("Implied − realized", _stat_num(vol.get("implied_minus_realized"), " pts")),
        ("MOVE (rates vol)", _stat_num(vol.get("move"))),
    ])


def _breadth_rows(breadth: Any) -> list[dict[str, str]]:
    if not isinstance(breadth, dict):
        return []
    markets = breadth.get("markets") if isinstance(breadth.get("markets"), list) else []
    primary = breadth.get("primary_market")
    row = next((m for m in markets if isinstance(m, dict) and m.get("market") == primary), None) or {}
    rows = _stat_rows([
        (f"{primary} breadth" if primary else "Breadth", _stat_num(row.get("breadth_pct"), "%")),
        (f"{primary} A/D ratio" if primary else "A/D ratio", _stat_num(row.get("ad_ratio"))),
        ("Net new highs", _stat_num(row.get("net_highs"), "", 0)),
        ("McClellan oscillator", _stat_num(breadth.get("mcclellan_osc"))),
    ])
    if breadth.get("warming_up"):
        rows.append({"label": "McClellan status", "value": "warming up — fewer than ~40 sessions persisted"})
    return rows


def _sentiment_rows(sentiment: Any) -> list[dict[str, str]]:
    if not isinstance(sentiment, dict):
        return []
    fg_score, fg_rating = sentiment.get("fg_score"), sentiment.get("fg_rating")
    fg_text = _stat_num(fg_score, "", 0) if fg_score is not None else None
    if fg_text and fg_rating:
        fg_text = f"{fg_text} ({fg_rating})"
    rows = _stat_rows([
        ("Fear & Greed", fg_text),
        ("F&G prior week", _stat_num(sentiment.get("fg_prev_week"), "", 0)),
    ])
    cot = sentiment.get("cot") if isinstance(sentiment.get("cot"), list) else []
    for row in cot[:3]:
        if not isinstance(row, dict):
            continue
        pct_oi = _stat_num(row.get("pct_oi"), "% OI")
        if pct_oi:
            rows.append({"label": f"COT — {row.get('label') or row.get('code') or '—'}", "value": pct_oi})
    return rows


def _valuation_split(valuation: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Rule 7: headline valuation figures as labeled stat rows (e.g. "CAPE
    42.56 · 99.4th percentile"); metrics with no source configured
    (`needs_source`) or no value drop into one muted "sources needed" list
    instead of an em-dash row each."""
    metrics = valuation.get("metrics") if isinstance(valuation, dict) else None
    if not isinstance(metrics, list):
        return [], []
    rows: list[dict[str, str]] = []
    needs_source: list[str] = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        label = m.get("label") or m.get("key") or "—"
        if m.get("needs_source") or m.get("value") is None:
            needs_source.append(str(label))
            continue
        text = _stat_num(m.get("value"))
        if text is None:
            needs_source.append(str(label))
            continue
        pctile = _num_or_none(m.get("percentile"))
        if pctile is not None:
            text = f"{text} · {pctile:.1f}th percentile"
        rows.append({"label": str(label), "value": text})
    return rows, needs_source


# ── quant: quantile-returns degenerate-table collapse ─────────────────────
def _quantile_returns_view(factor: dict[str, Any]) -> dict[str, Any]:
    """P1.2: when the cross-section is too small for a real
    quantile split, `mean_fwd_ret_pct` comes back `None` on EVERY bucket and
    `n_avg` is the same degenerate repeated float on every row (all buckets
    drew from the same single-name-per-date universe) — a 5-row table of
    em-dashes next to a suspiciously identical N column reads as broken, not
    "no data". Collapse to one sentence instead. `n_avg` is also rounded to
    2 decimals when the table DOES render (never the raw ~6-sig-fig float)."""
    rows = factor.get("quantile_returns") if isinstance(factor.get("quantile_returns"), list) else []
    if not rows:
        return {"rows": [], "collapsed": False}
    all_null = all(_num_or_none(r.get("mean_fwd_ret_pct")) is None for r in rows if isinstance(r, dict))
    if all_null:
        return {"rows": [], "collapsed": True}
    return {"rows": rows, "collapsed": False}


# ── shadow: warning-line dedup (banner + per-tool exceptions) ─────────────
# Substrings identifying the two boilerplate `_score_tool` warning shapes
# (`services/shadow.py`) that repeat near-verbatim across every graded tool
# once the whole book shares the same "too little history yet" state — the
# tool name is NOT embedded in these strings (added only at render time),
# so an exact string match across tools is a real signal of a shared cause,
# not a coincidence.
_SHADOW_BOILERPLATE_MARKERS = ("week(s) of shadow_verdicts history", "small sample (n=")


def _shadow_warnings_view(tools: dict[str, Any], grade_min_weeks: Any) -> dict[str, Any]:
    """P1.8: per-tool `low_confidence`/`warnings[]` lines
    used to repeat near-identically across every graded tool (4 tools x
    ~4 lines each, all saying the same "too little history yet" thing) —
    16-18 lines of redundant italic prose, none of it wrong, all of it
    noise. Tools sharing the majority (low_confidence, warnings) signature
    collapse into ONE banner line; a tool with warnings OUTSIDE that shared
    boilerplate (e.g. an overlap-veto note) still gets its own exception
    line. `effective_n`/`pushes` are no longer part of this prose at all —
    promoted to scoreboard table columns instead."""
    graded = {name: t for name, t in tools.items() if isinstance(t, dict) and t.get("graded")}
    if not graded:
        return {"banner": None, "exceptions": []}

    def is_boilerplate(w: str) -> bool:
        return any(m in w for m in _SHADOW_BOILERPLATE_MARKERS)

    def n_for(t: dict[str, Any]) -> float | None:
        n = _num_or_none(t.get("effective_n"))
        return _num_or_none(t.get("n_graded")) if n is None else n

    low_conf_names = [name for name, t in graded.items() if t.get("low_confidence")]
    banner = None
    banner_covers: set[str] = set()
    if len(low_conf_names) >= 2:
        banner_covers = set(low_conf_names)
        all_zero = all((n_for(graded[name]) or 0) == 0 for name in low_conf_names)
        zero_clause = "0 effective events, " if all_zero else ""
        weeks_clause = f"fewer than {_plural(grade_min_weeks, 'week')}" if grade_min_weeks is not None else "too little history"
        banner = (
            f"All {_plural(len(low_conf_names), 'graded tool')}: {zero_clause}{weeks_clause} of history "
            "— hit rates indicative only."
        )

    exceptions: list[dict[str, Any]] = []
    for name, t in graded.items():
        lines = [w for w in (t.get("warnings") or []) if isinstance(w, str) and not is_boilerplate(w)]
        if t.get("low_confidence") and name not in banner_covers:
            lines.append("low confidence (insufficient shadow_verdicts history)")
        if lines:
            exceptions.append({"tool": name, "text": "; ".join(lines)})
    return {"banner": banner, "exceptions": exceptions}


def _extract_chart_data(sections: dict, material_symbols: set[str]) -> dict[str, Any]:
    """Builds the chart-id -> spec map the vendored JS (`vendor/charts.js`)
    reads at render time, derived from the REAL producer fields (see module
    docstring) — the `portfolio`/`quant`/`holdings` chapters carry no
    dedicated `allocation`/`nav_history`/`factor_ic`/per-holding `sparkline`
    fields, so each chart is derived from whatever real field actually
    carries that information:
      - allocation donut <- top 8 + "Other" of `portfolio.positions[]` by
        value (`_allocation_breakdown`, rule 4).
      - "NAV/scorecard" line <- the frozen scorecard's two-arm cumulative
        TWR path (`actual_twr`/`do_nothing_twr`) — there is no NAV time
        series anywhere in this payload to plot instead.
      - regime history line <- `market.regime.history[].{date,score}` (new,
        rule 7 — replaces the raw daily-history dict dump).
      - factor IC bars <- `quant.factor.ic_series[].{date,ic}`.
      - per-holding sparkline <- `holdings.symbols[sym].technicals.
        series[].close`, MATERIAL symbols only (rule 8 — tear sheets, and
        therefore sparklines, exist only for material positions).
    Tolerant of missing/malformed chapter fields — a chart simply doesn't
    appear rather than raising."""
    charts: dict[str, Any] = {}

    portfolio = sections.get("portfolio") or {}
    if not portfolio.get("absent"):
        positions = portfolio.get("positions") or []
        if positions:
            slices, _legend = _allocation_breakdown(positions)
            charts["allocation_donut"] = {"type": "donut", "data": slices}
        scorecard = portfolio.get("scorecard") or {}
        actual_twr, do_nothing_twr = scorecard.get("actual_twr"), scorecard.get("do_nothing_twr")
        if actual_twr is not None or do_nothing_twr is not None:
            charts["nav_line"] = {
                "type": "lines",
                "series": [
                    {"name": "Actual", "points": [{"x": 0, "y": 0.0}, {"x": 1, "y": _num(actual_twr) * 100}]},
                    {"name": "Do-nothing", "points": [{"x": 0, "y": 0.0}, {"x": 1, "y": _num(do_nothing_twr) * 100}]},
                ],
            }

    market = sections.get("market") or {}
    if not market.get("absent"):
        regime = market.get("regime") if isinstance(market.get("regime"), dict) else {}
        history = regime.get("history") if isinstance(regime.get("history"), list) else []
        valid = [pt for pt in history if isinstance(pt, dict) and pt.get("score") is not None]
        if len(valid) >= 2:
            # P1.5: the chart data already carried the real
            # dates on every history point — the renderer just never read
            # them. `x_labels` (parallel to `points`, by sequential index)
            # lets `vendor/charts.js`'s `drawLines` paint a real date axis
            # (first/last + interior ticks) instead of an unlabeled line.
            charts["regime_history_line"] = {
                "type": "lines",
                "series": [{"name": "Regime score", "points": [
                    {"x": i, "y": _num(pt.get("score"))} for i, pt in enumerate(valid)
                ]}],
                "x_labels": [str(pt.get("date") or "") for pt in valid],
            }

    quant = sections.get("quant") or {}
    if not quant.get("absent"):
        factor = quant.get("factor") or {}
        ic_series = [pt for pt in (factor.get("ic_series") or []) if pt.get("ic") is not None]
        if ic_series:
            charts["factor_ic_bars"] = {
                "type": "bars",
                "data": [{"label": pt.get("date", ""), "value": _num(pt.get("ic"))} for pt in ic_series],
            }

    holdings = sections.get("holdings") or {}
    if not holdings.get("absent"):
        for symbol, entry in (holdings.get("symbols") or {}).items():
            if symbol not in material_symbols or not isinstance(entry, dict) or entry.get("absent"):
                continue
            series = ((entry.get("technicals") or {}).get("series")) or []
            closes = [_num(pt.get("close")) for pt in series if pt.get("close") is not None]
            if closes:
                charts[_spark_key(symbol)] = {"type": "sparkline", "points": closes}

    return charts


def _normalize_sections(sections: dict) -> tuple[dict, list[str]]:
    """Isinstance-guards every known structured field (list/dict) AND every
    known numeric leaf the template feeds through a `|pct`/`|num`/`|eur`
    filter before the template or `_extract_chart_data` ever sees the data.
    Wrong-shaped data is dropped (treated as missing, exactly like an absent
    field) and named in the returned warnings list as `<chapter>.<field>:
    expected <kind>, got <type>` (nested/per-item leaves are labeled with
    their path/symbol/tool, e.g. `holdings.symbols[BTC].technicals.rsi_14`).
    Unknown chapters/fields are untouched — `market`'s many-shaped bundle in
    particular is guarded at the point of use (see the `_market_*`/`_*_rows`
    readers above), not here (see the module-docstring note on
    `_CHAPTER_LIST_OF_DICT_FIELDS` for why)."""
    warnings: list[str] = []

    def warn(chapter_key: str, field: str, expected: str, value: Any) -> None:
        warnings.append(f"{chapter_key}.{field}: expected {expected}, got {type(value).__name__} — dropped")

    def as_list_of_dicts(chapter_key: str, field: str, value: Any) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            warn(chapter_key, field, "a list", value)
            return []
        if any(not isinstance(item, dict) for item in value):
            warn(chapter_key, field, "a list of objects", value)
        return [item for item in value if isinstance(item, dict)]

    def as_list(chapter_key: str, field: str, value: Any) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            warn(chapter_key, field, "a list", value)
            return []
        return value

    def as_dict(chapter_key: str, field: str, value: Any) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            warn(chapter_key, field, "an object", value)
            return {}
        return value

    def as_num(chapter_key: str, field: str, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)  # accepts int/float/numeric-string, e.g. "42.5"
        except (TypeError, ValueError):
            warn(chapter_key, field, "a number", value)
            return None

    def as_str(chapter_key: str, field: str, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            warn(chapter_key, field, "a string", value)
            return None
        return value

    normalized: dict[str, Any] = dict(sections or {})
    for key, _title in _CHAPTER_ORDER:
        chapter = normalized.get(key)
        if chapter is None:
            continue
        if not isinstance(chapter, dict):
            warn(key, "<chapter>", "an object", chapter)
            normalized[key] = {}
            continue
        if chapter.get("absent"):
            continue
        chapter = dict(chapter)

        for field in _CHAPTER_LIST_OF_DICT_FIELDS.get(key, ()):
            chapter[field] = as_list_of_dicts(key, field, chapter.get(field))
        for field in _CHAPTER_SCALAR_LIST_FIELDS.get(key, ()):
            chapter[field] = as_list(key, field, chapter.get(field))
        for field in _CHAPTER_DICT_FIELDS.get(key, ()):
            chapter[field] = as_dict(key, field, chapter.get(field))
        for field in _CHAPTER_NUMERIC_SCALAR_FIELDS.get(key, ()):
            if field in chapter:
                chapter[field] = as_num(key, field, chapter.get(field))
        for field in _CHAPTER_STRING_SCALAR_FIELDS.get(key, ()):
            if field in chapter:
                chapter[field] = as_str(key, field, chapter.get(field))

        # Per-item numeric leaves — runs after the list-of-dicts guard above,
        # so `chapter[list_field]` is already a clean list of dicts (or []).
        for list_field, numeric_fields in _LIST_ITEM_NUMERIC_FIELDS.get(key, {}).items():
            items = chapter.get(list_field)
            if not isinstance(items, list):
                continue
            fixed_items = []
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    fixed_items.append(item)
                    continue
                item = dict(item)
                label = item.get("symbol") or item.get("tool") or f"#{idx}"
                for nf in numeric_fields:
                    if nf in item:
                        item[nf] = as_num(key, f"{list_field}[{label}].{nf}", item.get(nf))
                fixed_items.append(item)
            chapter[list_field] = fixed_items

        # ── bespoke nested guards (real shapes aren't flat top-level fields) ──
        if key == "portfolio":
            var_cvar = chapter.get("var_cvar") or {}
            if isinstance(var_cvar, dict):
                var_cvar = dict(var_cvar)
                for nf in _VAR_CVAR_NUMERIC_FIELDS:
                    if nf in var_cvar:
                        var_cvar[nf] = as_num(key, f"var_cvar.{nf}", var_cvar.get(nf))
                coverage = var_cvar.get("coverage")
                if isinstance(coverage, dict):
                    coverage = dict(coverage)
                    for nf in _VAR_CVAR_COVERAGE_NUMERIC_FIELDS:
                        if nf in coverage:
                            coverage[nf] = as_num(key, f"var_cvar.coverage.{nf}", coverage.get(nf))
                    var_cvar["coverage"] = coverage
                elif coverage is not None:
                    var_cvar["coverage"] = as_dict(key, "var_cvar.coverage", coverage)
                chapter["var_cvar"] = var_cvar

            scorecard = chapter.get("scorecard") or {}
            if isinstance(scorecard, dict):
                scorecard = dict(scorecard)
                for nf in _SCORECARD_NUMERIC_FIELDS:
                    if nf in scorecard:
                        scorecard[nf] = as_num(key, f"scorecard.{nf}", scorecard.get(nf))
                chapter["scorecard"] = scorecard

        if key == "holdings":
            fixed_symbols: dict[str, Any] = {}
            for symbol, entry in chapter.get("symbols", {}).items():
                if not isinstance(entry, dict):
                    fixed_symbols[symbol] = entry
                    continue
                entry = dict(entry)
                if entry.get("absent"):
                    fixed_symbols[symbol] = entry
                    continue
                for field in _HOLDING_DICT_FIELDS:
                    if field in entry:
                        entry[field] = as_dict(key, f"symbols[{symbol}].{field}", entry.get(field))
                for field in _HOLDING_SCALAR_LIST_FIELDS:
                    if field in entry:
                        entry[field] = as_list(key, f"symbols[{symbol}].{field}", entry.get(field))
                technicals = entry.get("technicals") or {}
                if isinstance(technicals, dict):
                    technicals = dict(technicals)
                    technicals["series"] = as_list_of_dicts(
                        key, f"symbols[{symbol}].technicals.series", technicals.get("series"),
                    )
                    for pt in technicals["series"]:
                        if "close" in pt:
                            pt["close"] = as_num(key, f"symbols[{symbol}].technicals.series[].close", pt.get("close"))
                    if "rsi_14" in technicals:
                        technicals["rsi_14"] = as_num(key, f"symbols[{symbol}].technicals.rsi_14", technicals.get("rsi_14"))
                    entry["technicals"] = technicals
                valuation = entry.get("valuation") or {}
                if isinstance(valuation, dict):
                    valuation = dict(valuation)
                    for nf in ("price", "market_cap"):
                        if nf in valuation:
                            valuation[nf] = as_num(key, f"symbols[{symbol}].valuation.{nf}", valuation.get(nf))
                    entry["valuation"] = valuation
                forensic = entry.get("forensic") or {}
                if isinstance(forensic, dict):
                    forensic = dict(forensic)
                    forensic["cards"] = as_list_of_dicts(key, f"symbols[{symbol}].forensic.cards", forensic.get("cards"))
                    entry["forensic"] = forensic
                fixed_symbols[symbol] = entry
            chapter["symbols"] = fixed_symbols

        if key == "quant":
            factor = chapter.get("factor") or {}
            if isinstance(factor, dict) and not factor.get("absent"):
                factor = dict(factor)
                factor["ic_series"] = as_list_of_dicts(key, "factor.ic_series", factor.get("ic_series"))
                for pt in factor["ic_series"]:
                    if "ic" in pt:
                        pt["ic"] = as_num(key, "factor.ic_series[].ic", pt.get("ic"))
                factor["quantile_returns"] = as_list_of_dicts(key, "factor.quantile_returns", factor.get("quantile_returns"))
                for qr in factor["quantile_returns"]:
                    if "mean_fwd_ret_pct" in qr:
                        qr["mean_fwd_ret_pct"] = as_num(key, "factor.quantile_returns[].mean_fwd_ret_pct", qr.get("mean_fwd_ret_pct"))
                if "warnings" in factor:
                    factor["warnings"] = as_list(key, "factor.warnings", factor.get("warnings"))
                for scalar_field in ("ic_mean", "ic_ir", "ic_tstat", "ic_hit_rate", "long_short_spread"):
                    if scalar_field in factor:
                        factor[scalar_field] = as_num(key, f"factor.{scalar_field}", factor.get(scalar_field))
                chapter["factor"] = factor

            scoreboard = chapter.get("scoreboard") or {}
            if isinstance(scoreboard, dict):
                scoreboard = dict(scoreboard)
                rows = as_list_of_dicts(key, "scoreboard.rows", scoreboard.get("rows"))
                fixed_rows = []
                for row in rows:
                    row = dict(row)
                    label = row.get("signal_id", "?")
                    for nf in ("mean_rank_ic", "hit_rate"):
                        if nf in row:
                            row[nf] = as_num(key, f"scoreboard.rows[{label}].{nf}", row.get(nf))
                    fixed_rows.append(row)
                scoreboard["rows"] = fixed_rows
                if "warnings" in scoreboard:
                    scoreboard["warnings"] = as_list(key, "scoreboard.warnings", scoreboard.get("warnings"))
                chapter["scoreboard"] = scoreboard

        if key == "shadow":
            fixed_tools: dict[str, Any] = {}
            for tool_name, tool_data in chapter.get("tools", {}).items():
                if not isinstance(tool_data, dict):
                    fixed_tools[tool_name] = tool_data
                    continue
                tool_data = dict(tool_data)
                for nf in ("hit_rate", "benchmark_hit_rate", "hit_rate_wilson_lb"):
                    if nf in tool_data:
                        tool_data[nf] = as_num(key, f"tools[{tool_name}].{nf}", tool_data.get(nf))
                fixed_tools[tool_name] = tool_data
            chapter["tools"] = fixed_tools

        # Phase 18: `groups[]` is already a clean list of dicts (the
        # `_CHAPTER_LIST_OF_DICT_FIELDS` pass above ran first) — this guards
        # the two nested containers each group carries, `summary` (a flat
        # numeric-leaf dict) and `bins` (a list of dicts, or `None` — B2 —
        # which this loop leaves untouched; only a WRONG-TYPE non-list,
        # non-None `bins` gets coerced, same "drop to empty + warn" rule as
        # every other malformed list field).
        if key == "calibration":
            fixed_groups: list[Any] = []
            for idx, g in enumerate(chapter.get("groups", [])):
                if not isinstance(g, dict):
                    fixed_groups.append(g)
                    continue
                g = dict(g)
                label = f"{g.get('issued_by', '?')}/{g.get('horizon_days', idx)}"
                summary = g.get("summary")
                if isinstance(summary, dict):
                    summary = dict(summary)
                    for nf in _CALIBRATION_SUMMARY_NUMERIC_FIELDS:
                        if nf in summary:
                            summary[nf] = as_num(key, f"groups[{label}].summary.{nf}", summary.get(nf))
                    g["summary"] = summary
                elif summary is not None:
                    g["summary"] = as_dict(key, f"groups[{label}].summary", summary)
                bins = g.get("bins")
                if isinstance(bins, list):
                    fixed_bins = []
                    for b in bins:
                        if not isinstance(b, dict):
                            fixed_bins.append(b)
                            continue
                        b = dict(b)
                        for nf in _CALIBRATION_BIN_NUMERIC_FIELDS:
                            if nf in b:
                                b[nf] = as_num(key, f"groups[{label}].bins.{nf}", b.get(nf))
                        fixed_bins.append(b)
                    g["bins"] = fixed_bins
                elif bins is not None:
                    g["bins"] = as_list_of_dicts(key, f"groups[{label}].bins", bins)
                fixed_groups.append(g)
            chapter["groups"] = fixed_groups

        normalized[key] = chapter

    return normalized, warnings


def _safe_json(obj: Any) -> Markup:
    """JSON for embedding inside a `<script type="application/json">` tag.
    Escaping `</` prevents a `</script` sequence in the data (e.g. a news
    title) from closing the tag early; this is orthogonal to Jinja's
    autoescape (which HTML-escapes text nodes, not this raw JSON blob)."""
    raw = json.dumps(obj, default=str, ensure_ascii=False)
    return Markup(raw.replace("</", "<\\/"))


def _cover(sections: dict) -> dict[str, Any]:
    exec_summary = sections.get("exec_summary") or {}
    if exec_summary.get("absent"):
        return {}
    regime = exec_summary.get("regime")
    # Real shape (report_worker.py): `regime` is `market.regime`, a DICT
    # (`{"state": ..., "score": ..., ...}`), not a plain string.
    regime_state = regime.get("state") if isinstance(regime, dict) else regime
    portfolio = sections.get("portfolio") or {}
    var_cvar = (portfolio.get("var_cvar") or {}) if not portfolio.get("absent") else {}
    nav_eur = exec_summary.get("nav_eur")
    cash_eur = portfolio.get("cash_eur") if not portfolio.get("absent") else None
    cash_pct = (cash_eur / nav_eur * 100) if (nav_eur and cash_eur is not None) else None
    return {
        "nav_eur": nav_eur,
        "wow_pct": exec_summary.get("wow_pct"),
        "regime": regime_state,
        # Redesign (rule 1): the cover's KPI band adds VaR95/cash% alongside
        # the original NAV/WoW/regime — both sourced from the SAME portfolio
        # chapter the Portfolio Review reads, never recomputed.
        "var95_eur": var_cvar.get("var_eur"),
        "cash_pct": cash_pct,
    }


def _holding_scores(sections: dict) -> dict[str, str]:
    """Per-holding, merged Piotroski F-Score display string for the tear
    sheet's fixed-slot skeleton: a card
    used to print the numeric score in its fixed skeleton row AND, SEPARATELY,
    the forensic-cards loop printed the SAME piotroski card's verdict — "Score
    (Piotroski) 6.0" and "Piotroski F-Score Moderate" as two different rows on
    one card. One slot now: `"6.0 · Moderate"` (score-only or verdict-only
    when the other is missing, `"—"` when neither is present). The template's
    forensic-cards loop skips the piotroski card entirely
    (`_holdings_view`'s `other_forensic_cards`), so this is the only place it
    prints."""
    display: dict[str, str] = {}
    holdings = sections.get("holdings") or {}
    if holdings.get("absent"):
        return display
    for symbol, entry in (holdings.get("symbols") or {}).items():
        if not isinstance(entry, dict) or entry.get("absent"):
            continue
        cards = ((entry.get("forensic") or {}).get("cards")) or []
        card = next((c for c in cards if isinstance(c, dict) and c.get("key") == "piotroski"), None)
        score = _num_or_none(card.get("score")) if isinstance(card, dict) else None
        verdict = card.get("verdict") if isinstance(card, dict) else None
        parts = []
        if score is not None:
            parts.append(f"{score:.1f}")
        if isinstance(verdict, str) and verdict:
            parts.append(verdict)
        display[symbol] = " · ".join(parts) if parts else "—"
    return display


def _environment() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    env.filters["eur"] = _fmt_eur
    env.filters["spark_key"] = _spark_key
    env.filters["pct"] = _fmt_pct
    env.filters["num"] = _fmt_num
    env.filters["plural"] = _plural
    return env


def _render_html(sections: dict, report_week: str) -> str:
    normalized, render_warnings = _normalize_sections(sections)
    if render_warnings:
        _logger.warning(
            "report_pdf: %d wrong-shaped section field(s) for %s, rendered as missing: %s",
            len(render_warnings), report_week, "; ".join(render_warnings),
        )

    exec_summary = normalized.get("exec_summary") or {}
    portfolio = normalized.get("portfolio") or {}
    market = normalized.get("market") or {}
    holdings = normalized.get("holdings") or {}
    shadow = normalized.get("shadow") or {}
    calibration = normalized.get("calibration") or {}

    positions = [] if portfolio.get("absent") else (portfolio.get("positions") or [])
    top_positions, tail_position = _split_positions(positions)
    material_symbols = {p.get("symbol") for p in top_positions}
    _allocation_slices, allocation_legend = _allocation_breakdown(positions)
    # P2.14: WoW columns collapse to one caption when EVERY shown position
    # has no WoW figure at all (first snapshot week — nothing to compare
    # against yet), same collapse-when-absent pattern as the scorecard.
    wow_available = any(
        p.get("wow_weight_delta_pct") is not None or p.get("wow_value_delta_eur") is not None
        for p in top_positions
    )

    var_cvar = portfolio.get("var_cvar") or {}
    coverage = var_cvar.get("coverage") if isinstance(var_cvar.get("coverage"), dict) else {}

    cross_asset = market.get("cross_asset") if isinstance(market.get("cross_asset"), dict) else {}
    valuation_rows, valuation_needs_source = _valuation_split(market.get("valuation"))

    quant = normalized.get("quant") or {}
    factor = quant.get("factor") or {}

    cover = _cover(normalized)

    env = _environment()
    template = env.get_template("report_pack.html")
    vendor_js = (_TEMPLATE_DIR / "vendor" / "charts.js").read_text(encoding="utf-8")
    return template.render(
        sections=normalized,
        report_week=report_week,
        week_range=_week_date_range(report_week),
        cover=cover,
        chapter_order=_CHAPTER_ORDER,
        chart_data_json=_safe_json(_extract_chart_data(normalized, material_symbols)),
        charts_js=Markup(vendor_js),
        render_warnings=render_warnings,
        holding_scores=_holding_scores(normalized),
        plain_terms=_plain_terms(cover, exec_summary, portfolio, shadow),
        plain_terms_portfolio=_plain_terms_portfolio(portfolio, shadow.get("grade_min_weeks")),
        plain_terms_market=_plain_terms_market(market),
        plain_terms_holdings=_plain_terms_holdings(holdings, material_symbols),
        plain_terms_quant=_plain_terms_quant(quant),
        plain_terms_shadow=_plain_terms_shadow(shadow),
        plain_terms_calibration=_plain_terms_calibration(calibration),
        plain_terms_forecasts=_plain_terms_forecasts(normalized.get("forecasts") or {}),
        top5_positions=top_positions[:5],
        shadow_rollup=_shadow_rollup(shadow),
        calibration_groups=_calibration_groups_view(calibration.get("groups")),
        pv={
            "top": top_positions,
            "tail": tail_position,
            "wow_available": wow_available,
            "allocation_legend": allocation_legend,
            "var_cvar": var_cvar,
            "coverage": coverage,
            "coverage_sentence": _coverage_sentence(coverage),
            "scorecard": portfolio.get("scorecard") or {},
        },
        mv={
            "regime": market.get("regime") if isinstance(market.get("regime"), dict) else {},
            "regime_components": _regime_components_view(market.get("regime") if isinstance(market.get("regime"), dict) else {}),
            "cross_asset": cross_asset,
            "corr_pairs": _correlation_pairs(cross_asset),
            "calendar": _econ_calendar_rows(market),
            "volatility_rows": _volatility_rows(market.get("volatility")),
            "breadth_rows": _breadth_rows(market.get("breadth")),
            "sentiment_rows": _sentiment_rows(market.get("sentiment")),
            "valuation_rows": valuation_rows,
            "valuation_needs_source": valuation_needs_source,
        },
        hv=(
            {"material": [], "roster": []}
            if holdings.get("absent")
            else _holdings_view(holdings, positions, material_symbols)
        ),
        quantile_view=_quantile_returns_view(factor),
        shadow_warnings=_shadow_warnings_view(
            shadow.get("tools") if isinstance(shadow.get("tools"), dict) else {}, shadow.get("grade_min_weeks"),
        ),
        methodology_conventions=_methodology_conventions(portfolio),
    )


async def render_pack_pdf(sections: dict, report_week: str, out_dir: str = "/data/reports") -> str:
    """Renders `sections` to `<out_dir>/catallax-weekly-<report_week>.pdf` via
    headless Chromium (Playwright) and returns the written path.

    Playwright is imported lazily, inside the function body, on purpose: the
    main backend image never installs it (only `Dockerfile.report` does), so
    importing this module elsewhere (e.g. from the router that only needs
    `resolve_pdf_path`) must stay safe without it on the path."""
    from playwright.async_api import async_playwright  # noqa: PLC0415 (intentional lazy import)

    os.makedirs(out_dir, exist_ok=True)
    out_path = resolve_pdf_path(os.path.join(out_dir, f"catallax-weekly-{report_week}.pdf"), out_dir)
    html = _render_html(sections, report_week)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            # Explicit render-settle criterion (charts.js sets this synchronously
            # after drawing, or immediately if there's nothing to draw) — never a sleep.
            await page.wait_for_function(
                "document.documentElement.hasAttribute('data-render-done')", timeout=15000,
            )
            await page.emulate_media(media="print")
            await page.pdf(
                path=out_path,
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template=_HEADER_TEMPLATE.format(week=report_week),
                footer_template=_FOOTER_TEMPLATE,
                margin={"top": "24mm", "bottom": "20mm", "left": "15mm", "right": "15mm"},
            )
        finally:
            await browser.close()
    return out_path
