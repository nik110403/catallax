# Portfolio Advisor (Catallax, Phase 16)

Deterministic, config-driven rule engine over the portfolio. **No trade is ever
placed** — every rule evaluation produces a `recommendations` row for a human to
accept or override. Engine: `backend/app/services/advisor.py` (`AdvisorEngine`,
`make_advisor` seam). Config: `ADVISOR_*` in `.env.example` / `config.py`. Tables:
`portfolio_snapshots` + `recommendations` (Alembic `0011`, append-only). Report
assembly (`services/advisor_report.py` — weekly tick — and
`services/scorecard.py` + `routers/reports.py` — `/api/v1/reports/*`) has landed;
the anatomy below is read from that shipped code, not the design draft. Phase 18 adds
a lifecycle beyond `accept`/`override` (`modify`/`reject`/`execute`/`expired`/
`superseded`, `services/recommendation_lifecycle.py`) and a read-only actionability
projection (`services/decisions.py`) — see `## Lifecycle` below and
`skills/forecast-ledger.md` for the projection + the forecast/calibration machinery
that pairs with it.

Read this before touching `services/advisor.py`, `services/advisor_report.py`,
`services/scorecard.py`, `routers/reports.py`, the `Recommendation`/
`PortfolioSnapshot` models, or the Reports UI.

## The three rules

`AdvisorEngine.evaluate(as_of, report_week, overrides)` runs all three every call and
returns recommendation-row-shaped dicts — fired **and** non-fired, every evaluation is
an audit row (rule 3 dormant is the one exception: it emits nothing at all).

### 1. `weight_band_trim` — per-position band trim, ΔCVaR-gated

Every held position beyond its asset-class weight band (`ADVISOR_WEIGHT_BANDS`,
`CLASS:upper_frac`) is proposed for a trim back to the band edge — **hysteresis**:
it trims TO the edge, never past it to some lower target. Crypto gets its own
(wider) band so a volatile class does not fire every week. Every position gets its own row, in-band or not (in-band rows are
`action=hold, status=evaluated`, an audit trail of what didn't fire and why).

**Cash-equivalent holdings are excluded entirely** (`ADVISOR_CASH_EQUIVALENT_SYMBOLS`,
default `USDC,USDT,DAI,EURC` — design decision: the investment system is
WF-only and investment-only, stablecoins count as cash, not crypto positions): no
per-symbol row, no weight-band check, no "unclassified asset class" row either — they
never enter `current_weights` (the sell gate's weight vector) at all, same effect as
`RiskEngine.recompute_with_weights` imputing the leftover as cash. Instead ONE summary
row per evaluation (`action=hold, status=evaluated`, no `material` key — materiality
was never evaluated), e.g. `"cash-equivalents: 2 symbols, €1,000.00 — treated as
cash"`, so "every evaluation is a row" still holds without one row per stablecoin.

The **gain-trigger variant** (proposing a trim only on a real realised gain, using
actual `cost_basis`) is **live** (`SPEC-exporter.md` SE4 shipped): the Holdings
bridge carries `cost_basis` per asset (scaled by the exporter's own per-account
factor, so `value - cost_basis` is internally consistent), and the sell gate's tax
leg uses the real taxable gain (`value - cost_basis`, floored at 0) for any trimmed
leg that has it. **Absent-tolerant per leg, not all-or-nothing**: a leg without
basis (an old/cached exporter payload, or a position the exporter can't attribute
basis to) falls back to the prior conservative upper bound — the full trimmed value
as taxable gain — for that leg only, never blanking the real figure computed for the
rest. `tax_cost_arithmetic.cost_basis_available` is `True` only when **every**
trimmed leg on that row had real basis (a mixed trim's total tax figure is still
partly conservative, so the row must not claim to be fully live). Cost basis is a
**moving-average (gleitender Durchschnittspreis) approximation**, not Wealthfolio's
FIFO lot accounting — the tax label stays verbatim "indicative estimate, not tax
advice" regardless of whether basis is real or conservative.

**I1 (money-path hole, closed 2026-08-15)**: an asset with NO quote at all prices
off cost_basis at the exporter (`value == cost_basis`), which — before this fix —
looked like a genuine zero gain and could trim through the gate tax-free. The
exporter now marks that asset `"fallback": true` on `/holdings.by_asset[]`
(same contract `/positions-history` rows already carry); the bridge
(`WealthfolioBridge._fetch_holdings`) maps a `fallback: true` asset's `cost_basis`
to `None` before it ever reaches the advisor, so it takes the ordinary
absent-tolerant conservative path above, never a real-looking zero gain.

Gated by the sell gate (below); fires only if the gate fires AND the trim clears
materiality AND it isn't in cooldown.

A position whose `asset_class` isn't in `ADVISOR_WEIGHT_BANDS` at all gets its own
`action=hold, status=evaluated` row too (note: `"unclassified asset class <X> — no
band configured"`) — it used to be silently skipped (latent finding, controller
review), contradicting "every evaluation is a row"; this row carries no `material`
key (see the material-stamping invariant, Report anatomy below) since materiality was
never evaluated for it.

### 2. `cash_floor_band` — WF-only, investment-only, cash floor + regime band, **NOT** ΔCVaR-gated

**WF-only, investment-only** (design decision, supersedes the earlier
fix described below): the advisor's liquid measure and cash floor are
sized from Wealthfolio data alone — bank/ledger cash is excluded from
advisor input.

Target liquid **€** = `max(life floor €, regime band €)` when the life floor is
active, else the regime band alone (both legs converted to € before the `max` — the
band itself is configured as a fraction, but the comparison and the resulting
shortfall are always €, `advisor.py`'s `_rule_cash_floor`):
- **Life floor €** = `ADVISOR_LIFE_FLOOR_MULTIPLIER` × trailing-12-month average
  monthly `Expenses` (`LedgerBridge.query_rows()` BQL aggregate over the reporting
  currency). **Dormant by default** (`ADVISOR_LIFE_FLOOR_MULTIPLIER=0.0`) — at 0 the
  rule makes **NO ledger call at all**: the expenses BQL, the ledger-staleness
  computation, AND the `ADVISOR_CLEARING_ACCOUNTS` refusal guard below all exist
  solely to protect this ledger-derived leg, so all three go dormant together with
  it (no ledger inputs -> no guard needed). The row states this the same honest way
  DCA states its own dormancy: `"life floor inactive (multiplier 0) — targeting the
  regime cash band only"` prefixed onto the note, and `input_snapshot.life_floor_active
  = false`, `life_floor_eur`/`avg_monthly_expenses_eur`/`ledger_staleness_days` all
  `None` (never a silently-substituted 0). Setting the multiplier > 0 re-enables the
  whole leg — expenses BQL, staleness, AND the clearing guard — with no code change;
  the capability is retained, config-revivable, not removed. **M5 revival caveat**:
  re-enabling also revives the UNGUARDED ledger calls that leg makes (expenses BQL,
  staleness, clearing balance) — unlike rule 2's WF-only liquid measure, there is no
  absent-tolerant fallback here (that pattern was reverted with B2). A ledger outage
  while the life floor is active crashes that week's advisor tick run (logged; the
  weekly tick LOOP survives and retries next interval — see `_advisor_weekly_tick_loop`
  — but that week's report is skipped, not degraded). Something to know before flipping
  the multiplier back on, not a reason not to.
- **Regime band €** = `ADVISOR_CASH_BANDS` (`regime_state:band_frac`) looked up against
  `MarketResearchEngine.regime()`'s current state (risk-off/neutral/risk-on), then
  multiplied by **NAV** (the investment book — see the liquid-vs-NAV split below).

**Liquid measure = Wealthfolio cash + cash-equivalent holdings ONLY**
(`ADVISOR_CASH_EQUIVALENT_SYMBOLS`, e.g. stablecoins — reverts the earlier B2 fix,
which folded in ledger bank cash): the shortfall is `target_eur − liquid_eur`, where
`liquid_eur = cash_wf_eur + cash_equivalents_eur`. **NAV itself is explicitly
UNCHANGED** — it stays the Wealthfolio investment book only (still includes
cash-equivalent holdings' market value, since they're WF positions), so rule 1's
per-position weights (`value / nav`) never move because of this measure; only rule
2's own liquid-vs-target comparison does. The composition is carried on every row's
`input_snapshot`: `cash_wf_eur`, `cash_equivalents_eur`, `liquid_eur`, plus `cash_eur`
kept for backward compat (== `cash_wf_eur`) — there is no `cash_ledger_eur` key any
more (the reverted B2 field), not even a null placeholder.

**Refusal guard** (guards against a nonzero clearing balance) — **only checked when the life floor is
active**: before sizing anything, every account in `ADVISOR_CLEARING_ACCOUNTS` (and
its sub-accounts, prefix-matched) must sum to exactly 0. A nonzero sum short-circuits
to a single `action=refuse` row stating the sum — no sizing arithmetic runs. Ledger
staleness (`as_of` − last ledger txn date) is computed and carried in every row's
`input_snapshot` regardless of outcome, when the life floor is active.

Cash-equivalent holdings are also excluded from the pro-rata trim base — they're
already counted as liquid above, so "trimming" them to raise cash would be a no-op
(`investable = nav − cash_wf_eur − cash_equivalents_eur`; the `legs` list never
contains one). If cash already meets the target: `hold`. If short and there's
nothing investable to trim: `hold` with a note. Otherwise a pro-rata trim across
every non-cash-equivalent holding (`fraction = min(shortfall / investable, 1.0)`)
is proposed.

**This rule is explicitly NOT gated on the sell gate's ΔCVaR benefit** (controller
ruling): a pro-rata liquidity raise barely moves portfolio CVaR, so gating it there
would recreate a "dead rule" that (almost) never fires. A floor breach that clears
materiality + cooldown proposes the raise directly — its row still carries the full
tax/venue cost arithmetic, but as **labeled information for the human veto** (the
actual safety layer here), never as a firing condition.

**Cost gate**: "not ΔCVaR-gated" does not mean
"not cost-gated" — removing the ΔCVaR check must not leave rule 2
with no cost check at all (e.g. a fixed venue cost that dwarfs a small raise).
Two checks now run on the pro-rata trim, in order:
1. **Per-leg materiality**: each candidate leg (the pro-rata fraction of one
   holding) is dropped from the trim if it fails `_is_material()` — the SAME
   €/NAV% floor as the row-level check (`ADVISOR_MATERIALITY_EUR`/`_NAV_PCT`),
   applied leg-by-leg instead of only to the shortfall total. If any legs are
   dropped, the row sizes to what survives (`raise_eur` = achievable, not the
   full shortfall) and states the gap as `residual_eur` — never silently drops
   the difference. If EVERY leg is dropped, the row is `hold`/`evaluated` with
   a "nothing achievable this week" note, same posture as the pre-existing
   "no investable positions" case.
2. **Total-cost fraction** (`ADVISOR_COST_FRACTION_MAX`, default 0.10): the
   surviving legs' `tax_eur + venue_cost_eur` (`_tax_and_venue()`, same shared
   helper the sell gate uses) is compared to the achievable raise amount. If
   `cost_eur / raise_eur` exceeds the fraction, the rule does NOT propose —
   it emits an `evaluated`/`hold` row with the exact arithmetic in `note`
   ("cash-floor raise suppressed: cost €X = Y% of €Z raise exceeds N% floor")
   and `cost_fraction`/`cost_fraction_max` on `input_snapshot`. Since the tax
   leg falls back to the full trim value as taxable gain for any leg without
   real `cost_basis` (SE4's absent-tolerant fallback), a no-basis leg's tax
   alone (`ADVISOR_TAX_RATE`, default 0, configurable) can be material depending on the configured rate against a 10%
   fraction floor on its own — this is intentional conservatism, not a bug:
   the row still ships as an audit trail with the arithmetic, the accept path
   is what the human loses, not visibility into the near-miss.
`sizing` on a fired row is now `raise_eur` (what's actually achievable), not
the raw `shortfall_eur` — the pre-fix code could report a `sizing` larger
than what the trimmed legs actually summed to when the pro-rata fraction was
capped at 1.0 (investable < shortfall).

### 3. `dca_modulation` — dormant while `ADVISOR_DCA_BASE=0`

`ADVISOR_DCA_BASE` defaults to `0.0` (no investable income by default): the rule is a pure no-op — it emits **not even an `evaluated` row**.
`AdvisorEngine.rule_status()` reports it to report assembly as
`"inactive (no contribution schedule set)"` rather than pretending to advise.

Setting the base > 0 later activates it with no code change: contribution =
`ADVISOR_DCA_BASE × ADVISOR_DCA_REGIME_SCALE[regime_state]`, deployed (`action=buy`)
only when cash is above the regime band (below the band, `hold` — protects the
floor). A future sell-side deployment (selling above the cash band) would route
through the same ΔCVaR sell gate as rule 1; the buy-side path implemented today never
needs it (no realised gain on a buy).

## `positions_history()` contract (SE3/SE4 bridge, no consumer yet)

`WealthfolioBridge.positions_history(symbol, as_of=None)` (`services/wealthfolio.py`)
has no caller in this phase — it exists for the deferred v1.5 pre-launch-replay/grader
(`design-draft.md`). **I2**: stating its data-quality contract now, while the shape is
fresh, so that future consumer doesn't have to re-derive it:
- **Fallback days are DROPPED, not summed in cost-valued form.** A `fallback: true`
  row is excluded from the per-date aggregation entirely (the "never cost-valued"
  promise) — a date whose only rows are fallback rows contributes NOTHING to the
  series, a **hole**, never a value silently mixing market and cost prices.
- **An all-fallback symbol returns `[]`.** If every row for a symbol across its whole
  history is `fallback: true`, the aggregated series is empty — there is no partial,
  mostly-cost-valued series to fall back to.
- **The trailing forward-filled point may be WEEKS stale.** The exporter's series
  ends at the last snapshot/transaction date, not necessarily `as_of` — the bridge
  appends exactly one synthetic point at `as_of` carrying the last real aggregate,
  marked `stale=True` (a real series can lag by weeks). **Any consumer computing a return over the series MUST drop `stale=True`
  points first** — a carried-forward price is not a real observation and would
  understate/zero out the return for whatever period it falls in.

## Sell gate (rule 1, and any future sell-side DCA, only)

`benefit = Δweekly-CVaR95 scaled to the review horizon − expected-return give-up`;
`cost = indicative tax + venue cost`. Fires iff `benefit > cost × ADVISOR_BENEFIT_COST_RATIO`.

1. **Benefit**: `RiskEngine.recompute_with_weights()` computes weekly CVaR95 for
   the current weights and for the post-trim weights; `Δ = current_cvar_pct −
   post_cvar_pct`. Scaled to `ADVISOR_REVIEW_HORIZON_WEEKS` via **sqrt-time**
   (i.i.d. weekly-return assumption — the assumption string is echoed verbatim in
   every gate row as `scaling_assumption`), then converted to € against NAV:
   `benefit_risk_eur = Δcvar_pct/100 × nav × sqrt(horizon)`.
   `RISK_MAX_HOLDINGS`/`RISK_SKIP_SYMBOLS` (`risk.py`'s defensive cap + the
   unresolvable-symbol skip list) dilute the gate's **absolute** CVaR level — both
   `current_cvar_pct` and `post_cvar_pct` are computed over the same
   capped/skip-reduced universe, so the level each carries is systematically off by
   whatever that universe excludes. The **Δ** used for the benefit is comparatively
   unaffected (both sides share the same reduced universe, so the bias mostly
   cancels in the subtraction) — but any omission is still surfaced, never silent:
   `recompute_with_weights()`'s `warnings` fold into the gate row's own `warnings`
   list (`_sell_gate`, `advisor.py:432`).
2. **Give-up**: the opportunity cost of the trimmed exposure, netted out — trimmed €
   × `(ADVISOR_EXPECTED_RETURN_MAP[class] − ADVISOR_EXPECTED_RETURN_MAP[CASH]) ×
   (horizon / 52)`, summed across trimmed legs. `benefit_eur = benefit_risk_eur −
   giveup_eur`.
3. **Cost**: `tax_eur = taxable_gain × ADVISOR_TAX_RATE × (1 − ADVISOR_LOSS_OFFSET_FACTOR)`
   — `taxable_gain = max(trim_value − cost_basis, 0)` per leg when the leg's
   `cost_basis` is real (SE4, live), else the conservative upper bound (full trim
   value as taxable gain) for that leg — plus `venue_cost_eur` from `ADVISOR_VENUE_COST_MAP`
   (`CLASS:mode:value`; `flat` = €/order, `pct` = × trim value). Every cost payload
   carries the tax label **verbatim**: `"indicative estimate, not tax advice"`.
   **M1**: `ADVISOR_LOSS_OFFSET_FACTOR` now overlaps with the per-leg
   floor-at-0 above (SE4) — a loss leg already contributes ZERO taxable gain (not a
   negative one to offset), so `taxable_gain` for the book as a whole already has
   loss legs' relief baked in via the floor, leg by leg. Raising the factor above 0
   would apply loss-carryforward relief a SECOND time on top of that floor —
   double-counting, not additive. Keep it at `0.0` unless/until the tax model is
   redesigned to track actual loss-offset balances instead of this floor
   approximation.
4. **Threshold**: `cost_eur × ADVISOR_BENEFIT_COST_RATIO` (default 1.5×). Both sides
   are rounded to cents *before* comparing (`benefit_r > threshold_r`) so a published
   row can never show `benefit_eur == threshold_eur` with `fires: true` from
   unrounded dust.
5. **Materiality** (both required, `ADVISOR_MATERIALITY_EUR` / `_NAV_PCT`): the
   proposed amount must be ≥€50 **and** ≥0.5% of NAV. Below either floor, the rule
   still emits its row (`status=evaluated`, not `proposed`) with a "below
   materiality floor" note.
6. **Cooldown** (`ADVISOR_COOLDOWN_WEEKS`, default 4): a rule the user recently
   overrode stays quiet, keyed **per (rule_id, scope)** — `scope=symbol` for rule 1's
   per-position rows (`advisor.py:209`, an override on one symbol's trim must never
   suppress another symbol's), unscoped for rule 2 (`advisor.py:304`). **Rule 3 has no
   cooldown check at all** (`_rule_dca`, `advisor.py:335-364`, never calls
   `_cooldown_check`) — moot while it's dormant at `ADVISOR_DCA_BASE=0`, since it emits
   no rows to cool down in the first place. Suppression lifts early if the €-metric
   behind the row has since moved by ≥ the materiality € floor since the override (the
   arithmetic, not just the clock, drives re-surfacing). Cooldown state is supplied
   by the caller (`overrides: {cooldown_key: (report_week, metric_eur)}`) — the
   engine itself never queries the DB.

`RiskEngine.bundle()` is called **at most once** per `AdvisorEngine` instance, before
the first `recompute_with_weights()` call (the documented call-order contract) —
avoids a second rate-limited price fetch during the weekly run.

## Report anatomy (`advisor_report.py` + `scorecard.py`/`routers/reports.py`)

The weekly tick (`main.py`'s `_advisor_weekly_tick_loop`, polling every
`ADVISOR_TICK_INTERVAL_SECONDS`, gated by `ADVISOR_ENABLED`) calls
`generate_weekly_report()`, which gates on `report_due()` — an ISO-week-boundary
check reusing `strategy_runner.rebalance_due` exactly (`advisor_report.py:96-111`).
`report_week` (`"YYYY-Www"`, `iso_week()`) is the idempotent key: a run inside an
already-reported week makes **zero DB reads past `_last_report_week()`**, let alone a
write (`advisor_report.py:340-341`); a container down over one or more week
boundaries produces exactly one catch-up report on the next tick. A due run
snapshots every held symbol AND runs `AdvisorEngine.evaluate()` (cooldown context
sourced from stored `overridden` rows, `_load_overrides`), then persists
`portfolio_snapshots` + `recommendations` together in **one transaction**
(`generate_weekly_report`, `advisor_report.py:377-380`) — a week is all-or-nothing.

`assemble_report()` (`advisor_report.py:125-192`) builds four sections purely from
rows already in hand — no I/O — and is called from **two** places: the weekly tick (right
after persisting, `performance=None`) and the read route (`_assemble_for_week`,
`reports.py:114-164`, which re-reads the week's rows from the DB and additionally
computes `performance` via `scorecard.compute_scorecard()`). The two calls for the
same week are therefore NOT byte-identical to each other — only `scorecard.performance`
differs; everything else in the shape is:

- **`what_happened`** (`advisor_report.py:167-170`) — a **point-in-time snapshot**,
  NOT a week-over-week delta: `nav_eur`, `cash_eur`, `positions` (symbol/value_eur/qty),
  `regime_state`. There is no "what changed since last week" computation here.
- **`where_we_stand`** (`:171-177`) — the same `positions` list, plus
  `rule_evaluations`: **every** recommendation row from this run's `evaluate()` call
  (fired or not), summarised to `{rule_id, action, status, note, arithmetic}`. This is
  where the near-miss `evaluated` audit rows live, as one-line summaries — `arithmetic`
  (`_rule_arithmetic()`, an additive fifth key) is a curated, absent-tolerant slice of
  the row's `input_snapshot`/`tax_cost_arithmetic` (never the full snapshot, which can
  carry a large `legs` list) — the UI's would-fire-at figures without string-parsing
  `note`. **The `material` key on `arithmetic` is a load-bearing invariant**: it exists
  ONLY on rows where materiality was actually evaluated (an over-band trim or a cash
  shortfall) — never on an in-band `hold` or an unclassified-asset-class `hold` (see
  rule 1 above), both of which exit before `_is_material()` is ever called. **Do not
  stamp `material` onto those rows** — the UI keys its passing-hold-vs-near-miss
  distinction off the `material` KEY'S presence in `arithmetic` (present at all =
  materiality was evaluated for this row, so `material: false` is a genuine near-miss
  signal; absent = the row never got that far, an unconditional pass, not a value that
  happens to be `false`).
- **`what_to_do_next`** (`:178`) — `proposed` (rows with `status == "proposed"`) and
  `refused` (rows with `action == "refuse"`, i.e. the clearing-account guard firing)
  as **full row dicts** (arithmetic and all), plus `rule_status` (rule 3's
  active/inactive label).
- **`scorecard`** (`:179-183`) — `n_evaluated` (`len(recommendations)`), `n_proposed`,
  `n_refused`, `rule_status`, and `performance` (`None` on the write path; the read
  route merges in the real two-arm payload below under `scorecard.performance`).

### Scorecard performance (`services/scorecard.py`, computed only by the read route)

Two arms over `portfolio_snapshots` history, capped to the trailing
`HISTORY_MAX_WEEKS = 104` distinct weeks (`scorecard.py:50`, applied by
`reports.py:128-133`'s history query): **actual** (the portfolio's persisted `nav`
per week) vs **do-nothing** (buy-and-hold — freeze the earliest snapshot's quantities
+ cash, revalue only at prices implied by later snapshots; a symbol that fully exits
the actual book carries its last-seen price flat).

- Single stated convention (`CONVENTION`, `scorecard.py:52-69`, printed verbatim on
  every payload): **TWR, chain-linked over weekly sub-periods, each approximated via
  modified Dietz with cash flow `CF = 0`** — `portfolio_snapshots` carries no
  per-period external-flow figure yet (and `ADVISOR_DCA_BASE` is dormant at 0), so the
  convention string states plainly that a real external contribution/withdrawal in a
  week would be indistinguishable from investment return in these figures.
- `n_effective` (`compute_scorecard`, `scorecard.py:122-223`) counts **only** the
  sub-periods where the two arms' returns actually *differ* (`math.isclose` tolerance)
  — a tied sub-period is not evidence and is excluded, so 30 static weeks print
  `n_effective=0`, not `n=29`. `arms_coincide` is defined as exactly
  `n_effective == 0` — the same differ-check drives both, so a pure cash
  deposit/withdrawal week (quantities unchanged, but the actual book's NAV moves while
  the frozen basket doesn't) correctly counts as a divergence.
- `p_floor_two_sided = sign_test_two_sided_p_floor(n_effective) = min(1.0, 2**(1 - n))`
  for `n ≥ 1`, **`None`** at `n_effective == 0` — no comparison exists yet, never a
  faked small p-value.
- Explicit non-numeric states: `depth_weeks == 0` → `empty: true` with a "no history
  yet" note (**the** empty state); `depth_weeks == 1` → `empty: false` but
  `"insufficient history"` (need ≥2 weeks for any return). Both leave every numeric
  field `None` rather than 0.
- `assemble_report`'s `scorecard` dict (above) — `n_evaluated`/`n_proposed`/
  `n_refused`/`rule_status` — is a **separate counts block**, merged alongside
  `performance` under the same `"scorecard"` key; it is not part of `compute_scorecard`'s
  own return value.

**LLM narration is opportunistic, never load-bearing** (`_narrate`, `advisor_report.py:214-230`,
called from `generate_weekly_report`, `:389-395`): numbers are always computed in
Python first (`_build_facts`, the ONLY input the model sees); narration is attempted
only if `settings.llm_provider != "noop"`, and the **narration call itself is the
probe** (the `ai_probe.py` pattern — `health()` alone is `claude --version` and can't
detect a silently expired OAuth session). Any warning on the response (no-op
provider, missing binary, non-zero exit, timeout, unparseable output) or empty text
means "probe failed" — the report still ships in the same run, **visibly un-narrated**
(`assemble_report`'s `narrated: false`), with `narration_absent_reason` distinguishing
`"not configured in this deployment (ai profile not enabled)"` (`llm_provider == "noop"`,
no probe attempted at all) from `"narration probe failed (see backend logs)"` (a probe
was attempted and came back empty/warned) — `advisor_report.py:384-395`. A successful
narration persists onto the existing `ai_runs` table (`kind="narrate"`, no new table),
read back by `_load_narration` (`reports.py:97-111`). Wiring narration through a
dedicated `ai-runner` worker deployment is a tracked follow-up, not this phase.

**Read-route reassembly** (`_assemble_for_week`) has no record of whether a probe was
*attempted* for a given historical week — only whether an `ai_runs` row exists for
it — so it can't reuse the tick's inline two-state logic verbatim.
`advisor_report.narration_absent_reason_for_reassembly(settings, narration)` derives
the same two states from what reassembly CAN see (today's `settings.llm_provider` +
whether a row was found): `None` iff narrated; `"not configured..."` if
`llm_provider == "noop"`; else `"narration probe failed (see backend logs)"`. B1
finding: `_assemble_for_week` still hardcoded a single generic string regardless of
`llm_provider`, making the UI's "probe failed"/"not configured" branches
unreachable from the read route — `routers/reports.py` was left unchanged
here, so the reusable helper is shipped and unit-tested here; wiring the one
call-site line in `reports.py` to use it is a follow-up.

### Lifecycle: accept / modify / reject / override / execute (`services/recommendation_lifecycle.py`, `routers/reports.py`)

**Status vocabulary (`recommendation_lifecycle.STATUSES`/`TRANSITIONS`,
also documented on the `Recommendation` docstring's `status` column comment):**

```
evaluated → proposed → {accepted | modified | overridden | expired | superseded} → executed
```

**Transition table — the ONLY legal edges** (`recommendation_lifecycle.TRANSITIONS`);
everything else raises `InvalidRequest` naming both statuses:

| From | Legal targets | Writer |
|---|---|---|
| `proposed` | `accepted`, `modified`, `overridden`, `expired`, `superseded` | `accepted`/`modified`/`overridden` via router endpoint; `expired`/`superseded` **service only** (`expire_and_supersede`, no human-facing endpoint reaches them) |
| `accepted` | `executed` | router `/execute` |
| `modified` | `executed` | router `/execute` |
| `evaluated`, `overridden`, `expired`, `superseded`, `executed` | — (terminal or advisor-only) | — |

All values ≤ 16 chars (unconstrained `String(16)` — a future status still doesn't
need a migration, but any write path MUST go through
`recommendation_lifecycle.transition`, never a raw INSERT).

Router surface (`routers/reports.py`, all under `POST
/api/v1/reports/recommendations/{id}/<verb>`, response shape `{"new": _rec_dict,
"prior": _rec_dict}` for every verb): `accept` → `accepted`; `override` → `overridden`
+ `input_snapshot["human_action"]="override"`; **`modify`** (body: `note`,
`sizing_override`) → `modified`, storing the new `sizing` and preserving the original
under `input_snapshot["sizing_proposed"]`; **`reject`** (body: `note`) → `overridden`
+ `input_snapshot["human_action"]="reject"` (see F1 below — this is a status
**alias**, not a new status); **`execute`** (body: `executed_at`, `fill_price`,
`quantity`, `venue`, `note`; only legal from `accepted`/`modified`) → `executed`,
storing all four fields verbatim under `input_snapshot["execution"]`.

Every transition: `SELECT … FOR UPDATE` on the prior row (so two concurrent calls on
the same recommendation can't both observe the pre-transition status and fork the
chain by each inserting a row against the same `prior_id`); the prior row's `note`
(the rule's own firing arithmetic, e.g. "cash short of the €X target by €Y") carries
forward **unchanged** onto the new row; the human's reason lands separately under
`input_snapshot["human_note"]` — a new dict, never mutating `prior.input_snapshot` in
place, so the prior row stays byte-for-byte untouched. Under `DEMO_MODE` every write
path raises `DemoModeReadOnly` first (`recommendation_lifecycle.transition`'s first
line) — reads route through a separate synthetic/empty demo seam that never touches
the real DB.

**F1 — `reject` writes `overridden` (binding, do not "fix" without an unfreeze):**
the frozen `advisor_report._load_overrides` builds the advisor cooldown context from
rows with `status == "overridden"` **only**, and the frozen `advisor._cooldown_check`
consumes that context to suppress a rule re-firing on a just-rejected row. A distinct
`rejected` status would silently disable cooldown — the rule would re-fire every
week with no human ever told cooldown had stopped protecting them. So `/reject` is a
UI/endpoint **alias**: it persists `status="overridden"` exactly like `/override`,
distinguished only by `input_snapshot["human_action"]` (`"reject"` vs `"override"`) —
the chip in the UI reads "Rejected"/"Overridden" off that field, not off `status`. A
real, distinct `rejected` status is deferred to a **Phase-19 unfreeze** of
`advisor_report._load_overrides`/`advisor._cooldown_check` (both currently frozen for
the whole of Phase 18 — the acceptance criteria require an empty diff on `advisor.py`/
`advisor_report.py`/`scorecard.py`).

**`executed`** was a reserved-but-unwritten stage through Phase 17; the writer now
exists — an `accepted`/`modified` row transitions to it, recording
`input_snapshot["execution"] = {executed_at, fill_price, quantity, venue}`: the row
that means an actual trade went out. No rule ever writes `executed` itself.

**`expired`/`superseded`** are written only by
`recommendation_lifecycle.expire_and_supersede` (the report-worker's periodic
maintenance tick, alongside `services/forecast_resolver.py`'s resolution pass — see
`skills/forecast-ledger.md`): a `proposed` **leaf** row (not any other row's
`prior_id`) older than `RECOMMENDATION_EXPIRY_WEEKS` (falling back to
`ADVISOR_REVIEW_HORIZON_WEEKS` when `0`) past its `as_of` gets an `expired` row; a
`(rule_id, symbol-or-"")` scope with ≥2 `proposed` leaves gets all-but-the-newest
superseded. Idempotent — a rerun inserts nothing.

**The declared unfreeze (Phase 18 only, binding precedent):** the ground rule is
"frozen advisor core" = `advisor.py`/`advisor_report.py`/`scorecard.py`, empty diff
for the phase. `routers/reports.py` is **not** frozen, but its transition block
(`_transition`, `accept_recommendation`, `override_recommendation`, plus the new
`modify`/`reject`/`execute` handlers appended after them) was **declared unfrozen**
for this phase specifically so `_transition`'s body could be replaced with a call
into the new `recommendation_lifecycle` service and the three new verbs added;
everything else in `reports.py` (demo branches, `list_weeks`, `get_report*`, pack/PDF
reads, `_current_recommendations`, `_rec_dict`) stayed byte-identical — those two
helpers are reused **call-only**. Cite the affected **symbols** in any future
review/gate, not line numbers — they shift as items land.

**Decision-outcome projection:** what a leaf row actually means to act on right now
(funding source, risk-budget delta, invalidation/`still_valid`, review/expiry dates,
the full lifecycle chain) is **not** stored on the row — it's computed on read by
`services/decisions.DecisionProjection` (`GET /api/v1/reports/{week}/decisions`), a
Phase 18 addition. See `skills/forecast-ledger.md` for its contract; it reads this
same status vocabulary but never writes it.

## Append-only `recommendations` policy

`Recommendation` rows are **never UPDATEd**. A status transition is a **new row**
whose `prior_id` points at the row it supersedes (`NULL` on a lifecycle's first row).
`recommendation_lifecycle.transition` is the sole write path for every human-facing
verb; `AdvisorEngine._VALID_STATUSES` (unchanged, `{evaluated, proposed, accepted,
overridden, executed}`) is the smaller set the advisor engine itself ever emits when
evaluating rules — the lifecycle service's own `TRANSITIONS` table above is the
authority on what a *human* or the *maintenance tick* may do to a row afterward.

## The DEMO seam

`make_advisor(redis, settings) -> AdvisorEngine` is the **one** construction seam
(mirrors `make_wealthfolio`) — every call site must go through it, never
`AdvisorEngine(...)` directly. It already routes through `make_wealthfolio`/
`make_ledger`, which resolve to their `DEMO_MODE` fixtures on their own — so the
advisor is synthetic-by-construction under `DEMO_MODE` with zero branching in
`advisor.py` itself. A full demo advisor (fixture expense schedule so the demo life
floor is nonzero, synthetic narration) plugs in behind this same seam without
touching any call site.

## Config (`ADVISOR_*`, see `.env.example`)

`ADVISOR_ENABLED`, `ADVISOR_WEIGHT_BANDS`, `ADVISOR_CASH_EQUIVALENT_SYMBOLS`,
`ADVISOR_LIFE_FLOOR_MULTIPLIER`,
`ADVISOR_CASH_BANDS`, `ADVISOR_CLEARING_ACCOUNTS`, `ADVISOR_DCA_BASE`,
`ADVISOR_DCA_REGIME_SCALE`, `ADVISOR_REVIEW_HORIZON_WEEKS`,
`ADVISOR_BENEFIT_COST_RATIO`, `ADVISOR_MATERIALITY_EUR`,
`ADVISOR_MATERIALITY_NAV_PCT`, `ADVISOR_COOLDOWN_WEEKS`,
`ADVISOR_EXPECTED_RETURN_MAP`, `ADVISOR_VENUE_COST_MAP`, `ADVISOR_TAX_RATE`,
`ADVISOR_LOSS_OFFSET_FACTOR`, `ADVISOR_BOOTSTRAP_SEED` (reserved, unused in v1 —
seeds the v1.5 grader's block-bootstrap resampling; that grader itself is deferred).
