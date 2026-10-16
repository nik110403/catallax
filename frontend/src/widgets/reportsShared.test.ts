// Unit tests for the pure decision-join-key and local-datetime helpers in
// reportsShared.tsx (see RecommendationRow.tsx's
// `decisionJoinKey` comment for the full story).
//
// No test runner is configured in this repo (no vitest/jest, no `npm test`
// script — checked: package.json has neither). These are framework-free
// assertions, runnable by bundling with the project's own (already-
// installed, transitive-via-vite) esbuild and executing the bundle with
// node — never added as a new dependency. A throw on any failed assertion
// exits the process non-zero; a clean run falls through to the final
// console.log with no output above it.
//
//   node_modules/.bin/esbuild src/widgets/reportsShared.test.ts \
//     --bundle --platform=node --format=esm --outfile=/tmp/reportsShared.test.mjs \
//     && node /tmp/reportsShared.test.mjs
import type { DecisionRow, RuleEvaluation } from "../api/types";
import { decisionJoinKey, decisionSymbol, evalSymbol, localDatetimeInputDefault, localDatetimeInputToISOString } from "./reportsShared";

function assertEqual<T>(actual: T, expected: T, label: string): void {
  if (actual !== expected) {
    throw new Error(`FAIL ${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
  console.log(`ok — ${label}`);
}

// Minimal, fully-typed DecisionRow factory — only `rule_id`/`funding_source`
// vary per case; every other field is a plausible placeholder so the object
// literal satisfies the real interface (no `as DecisionRow` cast hiding a
// shape mismatch).
function makeDecisionRow(rule_id: string, funding_source: DecisionRow["funding_source"]): DecisionRow {
  return {
    id: 1,
    rule_id,
    action: "trim",
    status: "accepted",
    sizing: 100,
    as_of: "2026-08-31",
    report_week: "2026-W35",
    funding_source,
    risk_budget_impact: { unavailable: "test fixture" },
    invalidation: { text: "n/a", still_valid: null },
    review_at: "2026-09-07",
    expires_at: "2026-09-14",
    forecast: null,
    outcome: null,
    lifecycle: [],
  };
}

function makeRuleEvaluation(rule_id: string, symbol?: string): RuleEvaluation {
  return {
    rule_id,
    action: "trim",
    status: "accepted",
    note: null,
    arithmetic: symbol ? { symbol } : {},
  };
}

// ── Case 1: weight_band_trim carries a single-symbol "proceeds" funding
// source — the ONLY rule that gets a symbol-qualified key. ────────────────
{
  const decision = makeDecisionRow("weight_band_trim", { kind: "proceeds", symbols: ["MSFT"], est_proceeds_eur: 500, to: "cash", lots: "fifo" });
  const symbol = decisionSymbol(decision);
  assertEqual(symbol, "MSFT", "weight_band_trim: decisionSymbol returns the single proceeds symbol");
  assertEqual(decisionJoinKey(decision.rule_id, symbol), "weight_band_trim:MSFT", "weight_band_trim: join key is rule_id:SYMBOL");

  const evalRow = makeRuleEvaluation("weight_band_trim", "MSFT");
  assertEqual(
    decisionJoinKey(evalRow.rule_id, evalSymbol(evalRow)),
    decisionJoinKey(decision.rule_id, symbol),
    "weight_band_trim: decision-side and eval-side join keys agree",
  );
}

// ── Case 2: bare cash_floor_band — funding_source is "clearing_cash" (no
// symbols list at all), the multi-leg/no-proceeds shape. ──────────────────
{
  const decision = makeDecisionRow("cash_floor_band", { kind: "clearing_cash", account: "IBKR-EUR", amount_eur: 800, available_eur: 1200, covers: true });
  const symbol = decisionSymbol(decision);
  assertEqual(symbol, null, "cash_floor_band (clearing_cash): decisionSymbol is null");
  assertEqual(decisionJoinKey(decision.rule_id, symbol), "cash_floor_band", "cash_floor_band (clearing_cash): join key is the bare rule_id");
}

// ── Case 3: THE regression case — a single-leg cash_floor_band trim. Its
// funding_source is ALSO {kind:"proceeds", symbols:[ONE symbol]}
// (decisions.py::_funding_source builds cash_floor_band's `symbols` from its
// trim legs), which is shape-IDENTICAL to weight_band_trim's. The prior
// (buggy) `decisionSymbol` inferred the symbol from this shape alone and
// produced "cash_floor_band:SYM" here — but the eval side
// (advisor_report.py::_rule_arithmetic) never puts a top-level `symbol` on
// a cash_floor_band row, so evalSymbol is always null for it and the join
// silently missed. The fix gates on rule_id, so BOTH sides must agree on
// the bare key even when funding_source has exactly one proceeds symbol. ──
{
  const decision = makeDecisionRow("cash_floor_band", { kind: "proceeds", symbols: ["VWCE"], est_proceeds_eur: 300, to: "cash", lots: "fifo" });
  const symbol = decisionSymbol(decision);
  assertEqual(symbol, null, "cash_floor_band (single-leg proceeds): decisionSymbol must stay null — no shape-based guess");
  assertEqual(decisionJoinKey(decision.rule_id, symbol), "cash_floor_band", "cash_floor_band (single-leg proceeds): join key is the bare rule_id");

  const evalRow = makeRuleEvaluation("cash_floor_band"); // no top-level symbol, ever
  assertEqual(
    decisionJoinKey(evalRow.rule_id, evalSymbol(evalRow)),
    decisionJoinKey(decision.rule_id, symbol),
    "cash_floor_band (single-leg proceeds): decision-side and eval-side join keys now agree (the regression this test guards)",
  );
}

// ── Execute's "Executed at" default: built from LOCAL date components, and
// must round-trip through the submit-time conversion back to the SAME
// instant it was built from — the bug this guards against is a UTC-built
// default re-parsed as local on submit (hours off in non-UTC zones). ──────────
{
  const now = new Date();
  now.setSeconds(0, 0); // the input has minute precision only
  const defaultValue = localDatetimeInputDefault(now);
  assertEqual(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(defaultValue), true, "localDatetimeInputDefault: matches the ISO-shaped input format");
  const roundTripped = localDatetimeInputToISOString(defaultValue);
  assertEqual(roundTripped, now.toISOString(), "localDatetimeInputDefault → localDatetimeInputToISOString round-trips to the same instant");
}

console.log("ALL PASS");
