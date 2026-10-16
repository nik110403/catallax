# Forecast Ledger + Calibration (Catallax, Phase 18)

The Monday pack no longer only decides what to do — it also **issues gradeable
forecasts** and, on a later tick, **resolves** them against `daily_bars`. This is the
capability for evaluating prior judgment (the evaluation itself — enough resolved
weeks for the numbers to mean something — is time-gated). Tables: `forecasts` + `outcomes` (Alembic
`0013`), plus `shadow_verdicts.horizon_days`. Services: `services/forecasts.py`
(`ForecastLedger`, the ONLY writer of both tables), `services/forecast_resolver.py`
(`resolve_due`), `services/shadow.py` (`ShadowEngine.calibration`/
`chapter_calibration`, extended in place — the Phase 17 scoreboard chapter is
unchanged and rendered alongside), `services/decisions.py` (`DecisionProjection` —
the actionability contract on the `recommendations` chain; see
`skills/portfolio-advisor.md` for the status vocabulary it reads).

Read this before touching `models.Forecast`/`models.Outcome`, `services/forecasts.py`,
`services/forecast_resolver.py`, the calibration additions in `services/shadow.py`,
`routers/forecasts.py`, or the pack's `forecasts`/`calibration` chapters.

## Tables

**`forecasts`** — one row per issued, immutable forecast. `issued_by` names the
writer: `shadow:<tool>` (machine, issued from the weekly shadow scoreboard) or
`human` (minimal backend issuance, see below); `advisor:<rule_id>` is **reserved**
for Phase 19 — no writer exists this phase (advisor rules are risk actions, not
directional views — see `## Advisor forecasts are OUT this phase`). `statement` is a
sentence a stranger can judge true/false unaided; a `>`-comparator statement ends
"(exactly zero = no decision)" (a `push`, never a silent `False`). `target` +
`resolution_rule` are everything the resolver needs and nothing else. `status`:
`pending → resolved | abstained | void`; the ledger touches only this one column
after issuance — never the row's content. `UniqueConstraint(issued_by, subject,
decision_date, horizon_days)` makes weekly re-issue idempotent; `issue()` is `INSERT
… ON CONFLICT DO NOTHING`, never an UPDATE.

**`outcomes`** — one immutable resolution per forecast, `forecast_id` **UNIQUE** (a
forecast resolves once; a later restated bar never re-resolves it — policy, not just
a constraint). `hit`/`brier`/`log_score` are `NULL` on a push, on `uncovered`, or
when the forecast carried no `probability`. `attribution` judges **direction only**
in v1 (`{"thesis": "n/a", "direction": "hit"|"miss"|"push"|"n/a", "sizing": "n/a"}`
— the `"n/a"` is literal and rendered as such); `thesis` waits for Phase 19's journal
links, `sizing` for executed fills.

**`shadow_verdicts.horizon_days`** — nullableows stay `NULL` and are read as the engine's fixed `_GRADE_HORIZON_WEEKS * 7`.

## PIT (point-in-time) triple — verbatim from the ORM docstring (`models.Forecast`)

> **PIT (point-in-time) triple** — `published_at` (when the underlying evidence came
> into existence: for a `daily_bars:` forecast, the timestamp of the last daily bar
> the reading used, NOT the fetch time), `first_seen_at` (when Catallax's worker tick
> first observed that evidence) and `available_at` (when this row itself became
> usable by any downstream decision — its persistence time). The invariant
> `published_at <= first_seen_at <= available_at` is enforced by the issuing service
> AND by a DB CHECK constraint, since it is a correctness property of the ledger, not
> of one writer. This triple, plus `resolution_source`, is the point-in-time contract
> Phase 20's `market_events` extends — defined once, here.

For a `human` forecast all three collapse to "now" (a human statement is published
the moment it is typed). **As built:** `published_at` for a `shadow:<tool>`
forecast is not literally reachable at issuance time — neither `compute_verdicts`
nor `_shadow_readings` does bar I/O. It is approximated as the last point's date in
the symbol's `research.technicals` daily series for that week (the same OHLCV
backbone every shadow tool reads from), falling back to `build_start` (==
`first_seen_at`) when no series data exists. The PIT invariant still holds on the
fallback (equality, not violation) — this is a documented approximation, not a
deviation from the invariant itself.

## Issuance rules

One binary forecast per **gradeable** shadow verdict (`direction` not `NULL` — so
`forensic` and `hold` never issue, mirroring the scoreboard's own exclusions), built
by `services.forecasts.build_shadow_forecasts` and issued in the worker's per-pack
hook (`report_worker._issue_forecasts`, right after `sections["shadow"]`).

- **Rolling, non-overlapping windows (B3):** a forecast issues only when no `pending`
  forecast already exists for `(issued_by, subject)` at that horizon
  (`ForecastLedger.open_pending`). A call standing for 12 weeks yields 3 back-to-back
  28-day forecasts, not 12 overlapping ones; a flipped verdict issues immediately (the
  old pending row still resolves on its own terms). `evidence_refs` records
  `{"kind": "issuance_reason", "value": "new_call" | "rolled"}` — `rolled` when a
  **resolved** forecast already exists for the same `(issued_by, subject, direction)`
  (upgraded post-build by `report_worker._mark_rolled_forecasts`, which has the DB
  access the pure builder doesn't — **As built**: `build_shadow_forecasts`
  always tags `new_call` first; only the worker can see resolved history).
- **Cross-sectional targets:** `factor`/`signals` tools get
  `excess_return_vs_book_median > 0` (direction −1 flips the comparator to `<`);
  `technicals`/`patterns` get `forward_return > 0`. `resolution_rule.book_median` is
  set for cross-sectional tools only.
- **Idempotent re-issue:** a second `issue()` of the same rows reports
  `duplicates == len(rows)` and inserts nothing (`ON CONFLICT DO NOTHING` on the
  unique key).
- Issuance failures never sink the pack — wrapped exactly like the narration
  try/except in `_build_pack_body`; a failure is logged into
  `sections["forecasts"]["issuance_errors"]`, never raised out of the worker (an
  un-caught CHECK failure inside `issue()`'s batch loop would otherwise vanish
  silently — the whole reason `issue()` reports per-row `errors` instead of raising).

## Probability = Laplace over the issuer's own record, seeded from the scoreboard

`laplace_probability(hits, n) = (hits + 1) / (n + 2)` (rule of succession):
`(0,0) = 0.5`, `(3,4) = 2/3 ≈ 0.667`, `(7,8) = 0.8`.

A `shadow:<tool>` forecast's `probability` is that estimate over the issuer's
**effective graded record as of issuance**, frozen on the row forever after. The
record is, in principle, this ledger's own accumulating `outcomes ⋈ forecasts`
history (`ForecastLedger.stats_by_issuer`) **plus a one-time seed** read from the
legacy shadow scoreboard's `_score_tool` output for that tool at the current
`report_week` — `hits` and `n = effective_n if effective_n is not None else
n_decided`. The seed is recorded as `{"kind": "prior_seed", "source":
"shadow_scoreboard", "hits": h, "n": n, "report_week": w}` in `evidence_refs`; it is
**not** a back-filled forecast row (no row is created for the past) — a probability
input, fully disclosed on the row (the spec counts violations of "no retro-dated
forecasts" and this does not trip it). Below `forecast_probability_min_n` effective
observations, `evidence_refs` also gets `{"kind": "prior_note", "text": "effective
n=<n> < <min_n>: probability is a Laplace prior, not a track record"}`.

**As built:** `build_shadow_forecasts` is a pure, no-I/O helper and only
receives the scoreboard half (`scoreboard_stats`) — it has no access to the ledger's
own `stats_by_issuer` growth. At Phase-18 genesis (`outcomes` starts empty) this
collapses to exactly the documented seed, which is what the shipped acceptance
numbers test (seeded `hits=6/effective_n=8` → `p=7/10=0.7`; unseeded → `p=0.5`).
Summing the ledger's own accumulating history on top of the seed (once real
`outcomes` rows exist) is Phase-19-or-later follow-up work, not yet wired.

**Cross-sectional ½-weight (B4):** ledger observations from a cross-sectional issuer
(`factor`, `signals`) count **½ each** toward `n` — a tercile week pairs one
`add_watch` against one `trim_watch` from the same ranking, so the raw count
double-counts one independent comparison (mirrors `_score_tool`'s own
`effective_n = n // 2` halving). The same halving is carried into the calibration
summary's `effective_n` (see below) and into `stats_by_issuer`.

## Advisor forecasts are OUT this phase (M9/F3)

Trims/cash-floor/DCA are risk actions, not directional views, and per-rule `n` would
never reach `FORECAST_PROBABILITY_MIN_N`; `dca_modulation`'s `input_snapshot`
(`{cash_pct, band_pct, contribution_eur, regime_state, regime_scale}`) has no symbol
to forecast against. `advisor:<rule_id>` is reserved; the outcome check for a
recommendation this phase is the decision projection's `invalidation.still_valid` at
`review_at` (see `skills/portfolio-advisor.md` / `DecisionProjection`), never a price
forecast. Phase 19 may add projection-resolved decision-outcome claims under that
reserved issuer.

## Resolver + abstention policy (`services/forecast_resolver.py::resolve_due`)

Runs every worker tick (`_maintenance_tick`, after the demo/enabled guard and before
`_advisor_rows_exist`, never in demo mode), up to `FORECAST_RESOLVER_BATCH_SIZE`
`pending` forecasts with `due_date ≤ now`, ordered by `due_date`. `manual` forecasts
are **never** auto-resolved (skipped — only `POST /forecasts/{id}/resolve` closes
those out, and only when `resolution_source == "manual"`).

For `resolution_source` starting `daily_bars:`: `fetch_forward_return(...,
stale_days=FORECAST_RESOLVER_STALE_DAYS)` — entry = first close on/after
`decision_date` **and within `stale_days`** of it; exit = first close on/after
`due_date` **and within `stale_days`** of it. Either bound missed → `uncovered`
(forecast `abstained`, `attribution.direction = "n/a"`); this is a genuine change
from the pre-Phase-18 scoreboard, which only bounded the exit side with a fixed `+5`
tail (`FORECAST_RESOLVER_STALE_DAYS` parameterises that tail; the entry-side stale
bound is new — M8).

`matured` metrics: `forward_return = value`; `excess_return_vs_book_median = value −
book_median`, where **book_median** = median forward return over every symbol that
carries a verdict for that tool in that `report_week` — the tool's full
cross-section (`hold` and null-direction included, the same population
`_grade_tool`/`_score_tool` use), symbols pricing `pending`/`uncovered` excluded;
memoized per `(tool, report_week, horizon_days)` within one `resolve_due` call. A
`book_median` that comes back `None` (no priced cross-section that week) resolves the
forecast as `uncovered` rather than raising.

An **exactly-zero** metric is a **push**: `hit = None`, `attribution.direction =
"push"`, `brier` NULL, forecast status still `resolved` (a push is a completed,
gradeable event — not an abstention). Per-forecast exceptions are caught into
`errors` and never stop the batch.

## Calibration (`ShadowEngine.calibration` / `chapter_calibration`)

Extends the existing shadow scoreboard **in place** — `scoreboard()`/
`chapter_shadow()` are byte-identical, the new `calibration` chapter renders
alongside, not instead of, the shadow one. Grades `forecasts ⋈ outcomes` (one row
per issued forecast, not per verdict-change event), grouped by `issued_by` ×
`horizon_days`, over a rolling window `REPORT_PACK_CALIBRATION_WINDOW_WEEKS` weeks
back from the report week.

**B2 bins rule:** reliability bins (`_reliability_bins`, equal-width over `[0,1]`,
last bin closed `[lo, 1.0]`, empty bins listed with `n=0`, never dropped) are computed
**only when the group's decided probabilities span ≥ 2 bins** — a machine issuer's
near-constant per-week Laplace prior makes a reliability histogram tautological (one
populated bin forever, resolution = 0). Those groups get `bins = null` +
`bins_note` and should be read via discrimination (`hit_rate` vs
`benchmark_hit_rate`) and `brier_skill` instead. In practice: `shadow:<tool>` issuers
→ discrimination only; `human` → bins (varying probability by call).

**Reference (M5):** `brier_ref = p̄(1-p̄)` — the group's own realised hit base rate
(the climatological forecaster), **never** the flat 0.25 coin; `brier_coin = 0.25` is
kept as a labelled second column only, never the skill denominator.
`benchmark_brier` = Brier of a forecaster who always said `p = benchmark_hit_rate`;
`brier_skill = 1 - brier_mean / brier_ref` (`None` when `brier_ref == 0` or `n == 0`).
`effective_n = n // 2` for cross-sectional issuers, and `wilson_lb` uses it.
Pushes and abstentions are excluded from Brier and counted separately;
`abstention_rate = abstained / (n + pushes + abstained)` carries a fixed survivorship
caveat in `how_to_read` (m12 — `uncovered` correlates with delisting/dead tokens, not
missing-at-random). `low_confidence = effective_n < FORECAST_PROBABILITY_MIN_N`.

An empty `outcomes` table renders every issuer group that has `pending` forecasts
with `summary.n == 0`, `bins is None`, `pending > 0` — honest N=0, not an exception.

## Config keys (`.env.example`, grouped under "PHASE 18 — FORECAST LEDGER")

| Key | Type | Default | Meaning |
|---|---|---|---|
| `FORECAST_RESOLVER_BATCH_SIZE` | int | 200 | Max forecasts resolved per report-worker tick. |
| `FORECAST_RESOLVER_STALE_DAYS` | int | 10 | Max gap (days) between `decision_date`/entry bar AND `due_date`/exit bar; beyond it → `uncovered`. |
| `FORECAST_PROBABILITY_MIN_N` | int | 8 | Effective graded history below which an issued probability carries `prior_note`; also the calibration chapter's `low_confidence` threshold. |
| `REPORT_PACK_CALIBRATION_BINS` | int | 5 | Reliability bin count for varying-probability (human) issuers. |
| `REPORT_PACK_CALIBRATION_WINDOW_WEEKS` | int | 104 | How far back `calibration()` reads. |
| `RECOMMENDATION_EXPIRY_WEEKS` | int | 0 | Weeks after `as_of` an untouched `proposed` recommendation expires; `0` = fall back to `ADVISOR_REVIEW_HORIZON_WEEKS`. See `skills/portfolio-advisor.md` for the lifecycle this drives. |
| `FORECAST_LEDGER_START_DATE` | date | 2026-08-31 | Ledger floor: `ForecastLedger.issue()` rejects any row whose `decision_date` is earlier than this into `errors` — never inserted. Forcing a pre-phase week's pack (`force_run(now=…)`) therefore cannot back-fill forecasts. Added late in Phase 18 (2026-09-02). |

## No retro-dated forecasts

`forecasts.decision_date` must be ≥ the phase start (2026-08-31). Historic
`shadow_verdicts` are **not** back-filled into the ledger — they stay graded by the
existing scoreboard chapter, which is unchanged. Seeding a **probability prior**
from the scoreboard's aggregate hits/n (above) is explicitly not a back-fill: no
`forecasts` row is created for the past, and the seed is disclosed on the row via
`evidence_refs`. the spec counts any violation of this rule.

## Human issuance (minimal backend path ⨯ m15)

`ForecastLedger.issue_human` / `POST /api/v1/forecasts` / `POST
/api/v1/forecasts/{id}/resolve` exist as a **minimal backend-only** path — no UI
issue form this phase (see Deferrals below). This exists so the phase's build gate
(the spec) can prove issue → resolve → Brier → calibration chapter end-to-end on at
least one manual forecast without waiting for the time gate. `subject_kind`+
`subject`+`source` opts a human forecast into `daily_bars:` resolver coverage like
any machine forecast; omitting `source` makes it `manual` (macro/portfolio claims),
the only kind `/resolve` can close out by hand. Phase 19's analyst journal reuses
this same endpoint set as its "every entry emits a forecast" seam.

## Demo mode

Every new GET (`/api/v1/forecasts*`, `/api/v1/reports/{week}/decisions`) returns a
fixed, deterministic in-memory sample under `settings.demo_mode` — never a DB
session, never a `RiskEngine`/`ForecastLedger`/`DecisionProjection` construction
(the spec). Every write (`POST`) raises `DemoModeReadOnly` before touching
anything.

## Deferred to Phase 19 (tribune rev 2 — record in the 19 pack)

- Advisor-issued forecasts as projection-resolved decision-outcome claims
  (`advisor:<rule_id>` reserved).
- Subject-conditional probabilities for machine issuers (bucket by reading strength —
  would make reliability bins meaningful for `shadow:<tool>` groups).
- The human Issue-forecast **UI** form (the backend path above ships now; no
  `frontend/` client call for `issueForecast`/`resolveForecast` this phase).
- The Decisions **PDF** table (the `/reports/{week}/decisions` chapter and its UI
  card render live; the PDF chapter order stops at `calibration` — `sections["decisions"]`
  is present in the stored pack and simply ignored by `report_pdf.py`).
- A real `rejected` status distinct from `overridden` (needs the Phase-19 unfreeze of
  `advisor_report._load_overrides` — see `skills/portfolio-advisor.md`).
