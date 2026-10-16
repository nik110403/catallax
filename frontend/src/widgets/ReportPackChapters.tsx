// Weekly Report Pack chapters — seven collapsible document
// sections rendered inside ReportView, in the SAME report-as-document
// register `reportsShared.tsx`/`ReportView.tsx` already use (mono tabular
// numerals, serif prose captions, `<details>` for progressive disclosure —
// the same idiom the position list and scorecard's "full convention"
// disclosure already use, not a new accordion component). Deliberately NOT
// a registered widget — same reasoning as `RecommendationRow.tsx`: a weekly
// report is one document with one reading order, not a floating panel that
// could drift from it.
//
// Every field bound here is a REAL backend field name — read verbatim from
// `backend/app/services/report_pack.py` (chapters 2/3/4/5/7),
// `backend/app/services/shadow.py` (chapter 6), `backend/app/report_worker.py`
// (chapter 1 + the embedded factor-study payload) and
// `backend/app/adapters/lab/base.py::FactorReport` (the quant chapter's
// `factor` sub-payload — the same Pydantic model the Phase-11 Lab uses).
// Two chapter shapes are asymmetric between real and demo mode
// (no demo seam on RiskEngine/MarketResearchEngine/
// ResearchEngine/IntelService): the shadow chapter's real mode carries a
// graded scoreboard with no per-symbol `verdicts`; demo mode carries
// `verdicts` with every tool ungraded. Both render here, gracefully, from
// the SAME component — never two forked copies.
//
// Narration (fix round 5 — this comment was wrong twice over):
// `sections.exec_summary` carries an additive `narration` string once the
// worker's probe succeeds (`report_worker.py` fixes narration
// being write-only — generated, egressed, persisted to `ai_runs` — with no
// read surface ever showing it; `exec_summary.narration` is the one this
// file renders from) or a `narration_absent_reason` string when it didn't
// (disabled provider, probe failure) — mutually exclusive, both optional
// (older packs, or a pack built before this landed, have neither). Every
// string below (narration text, news headlines, rule notes, worker `reason`
// strings) is a plain JSX text child — React escapes it — and nothing in
// this file ever calls `dangerouslySetInnerHTML`.
import type {
  BreadthMarket,
  CalibrationGroup,
  CotRow,
  DecisionFundingSource,
  DecisionRiskBudgetImpact,
  DecisionRow,
  FactorReport,
  MethodologyTool,
  PackCalibrationChapter,
  PackDecisionsChapter,
  PackExecSummaryChapter,
  PackFactorPayload,
  PackForecastsChapter,
  PackHoldingsChapter,
  PackMarketChapter,
  PackPortfolioChapter,
  PackQuantChapter,
  PackQuantCrossSectionRow,
  PackShadowChapter,
  PackMethodologyChapter,
  PackSource,
  ReportPack,
  ScanPattern,
  ShadowToolGrade,
  ValuationMetric,
} from "../api/types";
import { isPackAbsent } from "../api/types";
import { theme } from "../theme";
import { PanelState } from "./PanelState";
import { ACTION_COLOR, ArithmeticGrid, PerformanceBlock, StatusBadge, eur, fmtValue, qty } from "./reportsShared";

// ── generic chrome ──────────────────────────────────────────────────────

// Design review fix round (2026-08-16, 27/40 → fix-first): every collapsible
// label in this file gets real heading semantics (P1 "44 disclosures carry
// no heading semantics") — h4 for a top-level chapter, h5 for everything
// nested inside one (market sub-sections, tear sheets, cross-section
// signals, methodology tools, the "how to read" caption). Visually inert
// (`margin:0`, sizes/weight inherited from the `<summary>` that wraps it) —
// this changes the accessibility tree, not the pixels.
function SummaryHeading({ level, children }: { level: 4 | 5; children: React.ReactNode }) {
  const Tag = (level === 4 ? "h4" : "h5") as "h4" | "h5";
  return (
    <Tag style={{ margin: 0, fontSize: "inherit", fontWeight: "inherit", lineHeight: "inherit", display: "inline" }}>
      {children}
    </Tag>
  );
}

// Same fix round — one hover + open-state rule shared by every `<summary>`
// in this file (className="pack-summary"), injected once. P2 "no hover
// state on any summary — 2 of 8 interactive states unaddressed": this rule
// covers every summary in the file with one declaration, and doubles as the
// open-state differentiator (`details[open] > summary`).
//
// Fix round 3: round 2's
// marker-restore approach (leave `<summary>` at its native `display:
// list-item`, move the space-between layout to an inner `width:100%` flex
// span) put the native marker and that 100%-wide child on separate lines —
// every chapter header rendered on two lines, an orphaned ▶ above the
// title. `ChapterDetails`' summary now goes back to `display:flex` (so it
// CAN share one line with a 100%-wide child) and gets an EXPLICIT chevron
// span instead of relying on the suppressed native marker — pure CSS
// rotation off the `<details open>` ancestor, no JS state. Nested summaries
// (`EngineCaption`, market sub-sections, tear sheets, methodology tools)
// keep their native marker — critique measured `display:list-item` on all
// of THOSE working correctly (no two-line defect); only `ChapterDetails`
// combines a marker with a 100%-wide flex child, so only it needs this.
const DISCLOSURE_CSS = `
.pack-summary { cursor: pointer; }
.pack-summary:hover { background: ${theme.panelHeader}; }
details[open] > summary.pack-summary { background: ${theme.panelHeader}; }
.pack-chapter-summary { list-style: none; }
.pack-chapter-summary::-webkit-details-marker { display: none; }
.pack-chevron { display: inline-block; font-size: ${theme.fontSize.micro}px; color: ${theme.muted}; flex-shrink: 0; }
details[open] > summary.pack-chapter-summary .pack-chevron { transform: rotate(90deg); }
@media (prefers-reduced-motion: no-preference) {
  .pack-chevron { transition: transform 0.15s ease; }
}
`;

function EngineCaption({ engine, howToRead }: { engine: string; howToRead: string }) {
  return (
    <details>
      <summary className="pack-summary" style={{ fontSize: theme.fontSize.micro, color: theme.muted, padding: "4px 2px" }}>
        <SummaryHeading level={5}>
          how to read this chapter — <span className="num">{engine}</span>
        </SummaryHeading>
      </summary>
      <div style={{ fontFamily: theme.fontSerif, fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4, lineHeight: 1.4 }}>
        {howToRead}
      </div>
    </details>
  );
}

function ChapterDetails({
  title,
  absentReason,
  source,
  defaultOpen,
  id,
  children,
}: {
  title: string;
  absentReason: string | null;
  source?: PackSource;
  defaultOpen?: boolean;
  // Phase 18: an anchorable id — `ReportView`'s "Forecasts pending"
  // strip jumps + force-opens this exact element (`el.open = true` before
  // `scrollIntoView`, since a closed `<details>`'s content isn't visible
  // even once scrolled to).
  id?: string;
  children?: React.ReactNode;
}) {
  return (
    <details id={id} open={defaultOpen} style={{ border: `1px solid ${theme.border}`, borderRadius: 4 }}>
      {/* Fix round 3: `display:flex` so the chevron + title + status chips
          all share ONE line (a 100%-wide flex child cannot share a line
          with a native marker box — that was round 2's two-line defect).
          The chevron below is explicit and rotates on `[open]`; the native
          marker is suppressed for this summary only (`.pack-chapter-summary`). */}
      <summary
        className="pack-summary pack-chapter-summary"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          // Fix round 4 (small item): round 2's measured ~40px hit target
          // was actually the two-line wrap defect, not real padding — fixing
          // the wrap (round 3) silently regressed the click target back to
          // ~26px. Real vertical padding restores it honestly.
          padding: "12px 10px",
          fontSize: theme.fontSize.sm,
          color: theme.text,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span className="pack-chevron" aria-hidden="true">▶</span>
          <SummaryHeading level={4}>{title}</SummaryHeading>
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
          {source?.stale && (
            <span style={{ fontSize: theme.fontSize.micro, color: theme.warn, border: `1px solid ${theme.warn}`, borderRadius: 3, padding: "0 5px", textTransform: "none" }}>
              stale
            </span>
          )}
          {source?.partial && (
            <span style={{ fontSize: theme.fontSize.micro, color: theme.warn, border: `1px solid ${theme.warn}`, borderRadius: 3, padding: "0 5px", textTransform: "none" }}>
              partial
            </span>
          )}
        </span>
      </summary>
      <div style={{ padding: "8px 10px", borderTop: `1px solid ${theme.grid}` }}>
        {absentReason !== null ? <PanelState kind="empty" message="Not available for this report" hint={absentReason} /> : children}
      </div>
    </details>
  );
}

// A generic flat table for arrays of already-typed rows — the SAME
// generic-render philosophy `ArithmeticGrid`/`fmtValue` already document for
// per-rule JSONB blobs, applied to per-row arrays this file needs across
// market/quant/holdings without hand-rolling five near-identical grids.
// P2 fix: a column is numeric (and right-aligned, header included) the
// instant ANY row has a number there — matching `PortfolioBody`'s
// hand-rolled table and the rest of the terminal's own right-alignment law,
// rather than left-aligning decimals that then fail to line up.
function FlatTable<T extends object>({
  rows,
  columns,
  rowKey,
}: {
  rows: T[];
  columns: { key: keyof T; label: string; width?: string; format?: (v: unknown) => React.ReactNode }[];
  rowKey: (row: T, i: number) => string;
}) {
  if (rows.length === 0) return null;
  const template = columns.map((c) => c.width ?? "1fr").join(" ");
  const numericCols = new Set(columns.filter((c) => rows.some((r) => typeof r[c.key] === "number")).map((c) => c.key));
  return (
    <div style={{ border: `1px solid ${theme.grid}`, borderRadius: 4, overflowX: "auto" }}>
      <div style={{ display: "grid", gridTemplateColumns: template, gap: 8, padding: "2px 8px", fontSize: theme.fontSize.micro, color: theme.muted, borderBottom: `1px solid ${theme.grid}` }}>
        {columns.map((c) => (
          <span key={String(c.key)} style={{ textAlign: numericCols.has(c.key) ? "right" : "left" }}>
            {c.label}
          </span>
        ))}
      </div>
      {rows.map((r, i) => (
        <div key={rowKey(r, i)} style={{ display: "grid", gridTemplateColumns: template, gap: 8, padding: "2px 8px", fontSize: theme.fontSize.micro, borderBottom: `1px solid ${theme.grid}` }}>
          {columns.map((c) => (
            <span
              key={String(c.key)}
              className={typeof r[c.key] === "number" ? "num" : undefined}
              style={{ color: theme.text, textAlign: numericCols.has(c.key) ? "right" : "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {c.format ? c.format(r[c.key]) : fmtValue(r[c.key])}
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

const pct1 = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const pp2 = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}pp`);

// Fix round 3 (MUST-FIX 2): the live risk bundle's `coverage` sub-object
// (`RiskCoverage` — n_assets_total/n_covered/n_skipped/covered_weight_pct/
// by_reason/…) is embedded UNTYPED inside `var_cvar` here (this chapter's
// own comment: "the live risk bundle's own shape is Phase-8.5's, not this
// chapter's contract") — passed whole to `ArithmeticGrid`, it rendered
// through `fmtValue`'s `JSON.stringify` fallback as a ~600-char blob in a
// 150px column. Same remedy as the FACTOR STUDY leak: exclude the nested
// object from the grid, render it as one sentence instead. Reads fields
// defensively (`unknown`, not a cast to a formal type) since this data's
// own contract is intentionally loose.
function coverageSentence(coverage: unknown): string | null {
  if (!coverage || typeof coverage !== "object") return null;
  const c = coverage as Record<string, unknown>;
  const total = c.n_assets_total;
  const covered = c.n_covered;
  if (typeof total !== "number" || typeof covered !== "number") return null;
  let s = `covers ${covered} of ${total} position${total === 1 ? "" : "s"}`;
  if (typeof c.covered_weight_pct === "number") s += `, ${c.covered_weight_pct.toFixed(1)}% of weight`;
  if (typeof c.n_skipped === "number" && c.n_skipped > 0) s += `; ${c.n_skipped} skipped`;
  return s;
}

// Fix round 3 (small item): the generic `ArithmeticGrid` bypassed the
// house's own already-established conventions for two obvious, unambiguous
// key shapes — a `*_pct` field in this risk-bundle/quant data is already
// 0–100 scaled (the SAME convention `weight_pct`/`wow_weight_delta_pct`
// already use elsewhere in this very file — never re-multiplied through
// `pctFrac()`, which expects a 0–1 fraction and would 100x it), and an
// ISO-timestamp string (`as_of`, `fetched_at`, …) reads better trimmed to
// seconds than with a raw fractional-second UTC offset trailing it. Applied
// only where the key/value SHAPE makes the meaning unambiguous — everything
// else passes through untouched, per "don't over-engineer".
const ISO_TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;
function humanizeGridFields(data: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    if (typeof v === "number" && k.toLowerCase().endsWith("_pct")) {
      out[k] = `${v.toFixed(2)}%`;
    } else if (typeof v === "string" && ISO_TIMESTAMP_RE.test(v)) {
      out[k] = v.slice(0, 19).replace("T", " ");
    } else {
      out[k] = v;
    }
  }
  return out;
}

// ── ch1: executive summary ───────────────────────────────────────────────
function ExecSummaryBody({ c }: { c: PackExecSummaryChapter }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      <div style={{ fontFamily: theme.fontSerif, fontSize: theme.fontSize.base, color: theme.text }}>{c.headline}</div>
      {/* Fix round 5 (SEV1): narration was
          write-only until this landed on `sections.exec_summary` — generated,
          egressed, persisted to `ai_runs`, with no read surface ever showing
          it. Plain JSX text child only (it may quote untrusted headlines;
          React escapes it, no `dangerouslySetInnerHTML`), same serif document
          voice the top-level report's own `NarrationPreamble` uses. */}
      {c.narration ? (
        <div style={{ fontFamily: theme.fontSerif, fontSize: theme.fontSize.base, color: theme.text, lineHeight: 1.5, whiteSpace: "pre-line" }}>
          {c.narration}
        </div>
      ) : c.narration_absent_reason ? (
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>Not narrated: {c.narration_absent_reason}</div>
      ) : null}
      {/* P3 fix: the six "portfolio: present / market: present / …" chips
          restated the chapter list immediately below them (each chapter
          already shows its own name, open or absent, one scroll down) —
          dropped rather than kept as noise. */}
      {Object.keys(c.shadow_counts).length > 0 && (
        <div>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", marginBottom: 4 }}>SHADOW ROLL-UP</div>
          <div className="num" style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px", fontSize: theme.fontSize.micro, color: theme.muted }}>
            {Object.entries(c.shadow_counts).map(([tool, t]) => (
              <span key={tool}>
                {tool}: {t.n_events ?? "—"} event{t.n_events === 1 ? "" : "s"} ({t.graded ? "graded" : "ungraded"})
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── ch2: portfolio review ────────────────────────────────────────────────
function PortfolioBody({ c }: { c: PackPortfolioChapter }) {
  const sorted = [...c.positions].sort((a, b) => b.value_eur - a.value_eur);
  // P1 fix: mirror `ReportView.tsx`'s own "What happened" position list —
  // top 10 by value inline, the rest one click away — instead of the
  // 56-row, 1,710px wall this chapter previously rendered unconditionally.
  const head = sorted.slice(0, 10);
  const rest = sorted.slice(10);
  const row = (p: (typeof sorted)[number]) => (
    <div
      key={p.symbol}
      style={{ display: "grid", gridTemplateColumns: "1fr 80px 100px 100px 90px", gap: 8, padding: "2px 8px", fontSize: theme.fontSize.micro, borderBottom: `1px solid ${theme.grid}` }}
    >
      <span style={{ color: theme.text }}>{p.symbol}</span>
      <span className="num" style={{ textAlign: "right", color: theme.muted }}>{p.weight_pct.toFixed(2)}%</span>
      <span className="num" style={{ textAlign: "right", color: theme.text }}>{eur(p.value_eur)}</span>
      <span className="num" style={{ textAlign: "right", color: p.wow_value_delta_eur == null ? theme.muted : p.wow_value_delta_eur >= 0 ? theme.up : theme.down }}>
        {p.wow_value_delta_eur == null ? "—" : `${p.wow_value_delta_eur >= 0 ? "+" : ""}${eur(p.wow_value_delta_eur)}`}
      </span>
      <span className="num" style={{ textAlign: "right", color: p.wow_weight_delta_pct == null ? theme.muted : p.wow_weight_delta_pct >= 0 ? theme.up : theme.down }}>
        {pp2(p.wow_weight_delta_pct)}
      </span>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      {/* P1 fix (reading layer): NAV promoted to the document hero size —
          the one figure that matters in this chapter — everything else
          alongside it stays agate. */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: theme.text }}>{eur(c.nav_eur)}</span>
        <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          NAV · CASH {eur(c.cash_eur)}
          {c.prior_report_week && <> · vs {c.prior_report_week}</>}
        </span>
      </div>
      {sorted.length === 0 ? (
        <PanelState kind="empty" message="No held positions this week" />
      ) : (
        <div style={{ border: `1px solid ${theme.grid}`, borderRadius: 4 }}>
          {head.map(row)}
          {rest.length > 0 && (
            <details>
              <summary className="pack-summary" style={{ padding: "4px 8px", fontSize: theme.fontSize.micro, color: theme.muted }}>
                {rest.length} more position{rest.length === 1 ? "" : "s"} (by value)
              </summary>
              {rest.map(row)}
            </details>
          )}
        </div>
      )}
      {isPackAbsent(c.var_cvar) ? (
        <PanelState kind="empty" message="VaR/CVaR unavailable" hint={c.var_cvar.reason} />
      ) : c.var_cvar ? (
        <div>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", marginBottom: 4 }}>VAR / CVAR</div>
          {/* MUST-FIX 2: `coverage` is excluded from the grid (it's a nested
              RiskCoverage object, not a scalar) and rendered as its own
              sentence instead — was a ~600-char JSON blob in a 150px column. */}
          <ArithmeticGrid data={humanizeGridFields(c.var_cvar)} exclude={["coverage"]} />
          {(() => {
            const sentence = coverageSentence((c.var_cvar as Record<string, unknown>).coverage);
            return sentence && <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4 }}>{sentence}</div>;
          })()}
        </div>
      ) : null}
      {c.coverage && (
        <div>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", marginBottom: 4 }}>RISK COVERAGE</div>
          {(() => {
            const sentence = coverageSentence(c.coverage);
            return sentence && <div style={{ fontSize: theme.fontSize.micro, color: theme.text, marginBottom: 4 }}>{sentence}</div>;
          })()}
          <ArithmeticGrid data={humanizeGridFields(c.coverage)} exclude={["by_reason"]} />
        </div>
      )}
      <PerformanceBlock perf={c.scorecard} />
    </div>
  );
}

// ── ch3: market context — no fixed shape beyond the sub-bundles themselves
// (report_pack.py's own docstring: "no fixed shape... generic key/value
// fallback"), so every sub-section here renders descriptively rather than
// as a chart — the live Market Research dashboard elsewhere owns the charts;
// this is the document appendix reading of the same data. ──────────────────
function MarketBody({ c }: { c: PackMarketChapter }) {
  const calEnv = c.econ_calendar_week_ahead;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />

      {c.regime && (
        <details open>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>
              Regime — <span className="num">{c.regime.state}</span>
              {c.regime.score !== null && <span className="num"> ({c.regime.score.toFixed(2)})</span>}
              {c.regime.recession_flag && <span style={{ color: theme.down }}> · recession flag</span>}
            </SummaryHeading>
          </summary>
          <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 6 }}>
            {/* P1 fix (reading layer): the regime line is this chapter's one
                key figure — promoted to the document hero size. */}
            <div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: theme.text }}>
              {c.regime.state}
              {c.regime.score !== null && <> ({c.regime.score.toFixed(2)})</>}
            </div>
            {/* P2 fix: state weight coverage explicitly — the shadow
                chapter's rigor (never a silent partial reading) extended
                here, rather than printing a composite score while half its
                component weight has no value and no contribution. */}
            {(() => {
              const totalWeight = c.regime.components.reduce((s, cm) => s + cm.weight, 0);
              const covered = c.regime.components.filter((cm) => cm.value !== null);
              const coveredWeight = covered.reduce((s, cm) => s + cm.weight, 0);
              return (
                <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                  {covered.length}/{c.regime.components.length} components covered
                  {totalWeight > 0 && <> ({((coveredWeight / totalWeight) * 100).toFixed(0)}% of weight)</>}
                </div>
              );
            })()}
            <FlatTable
              rows={c.regime.components}
              rowKey={(r) => r.key}
              columns={[
                { key: "label", label: "COMPONENT", width: "1.4fr" },
                { key: "value", label: "VALUE", width: "80px" },
                { key: "score", label: "SCORE", width: "70px" },
                { key: "weight", label: "WEIGHT", width: "70px" },
                { key: "contribution", label: "CONTRIB", width: "80px" },
              ]}
            />
          </div>
        </details>
      )}

      {c.cross_asset && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>
              Cross-Asset — avg pairwise corr {c.cross_asset.avg_pairwise_corr === null ? "—" : c.cross_asset.avg_pairwise_corr.toFixed(2)}
            </SummaryHeading>
          </summary>
          <div style={{ marginTop: 6 }}>
            <FlatTable
              rows={c.cross_asset.snapshot}
              rowKey={(r) => r.symbol}
              columns={[
                { key: "symbol", label: "SYM", width: "70px" },
                { key: "label", label: "NAME", width: "1.2fr" },
                { key: "last", label: "LAST", width: "80px" },
                { key: "chg_1d_pct", label: "1D%", width: "70px" },
                { key: "chg_1w_pct", label: "1W%", width: "70px" },
                { key: "chg_1m_pct", label: "1M%", width: "70px" },
                { key: "chg_ytd_pct", label: "YTD%", width: "70px" },
              ]}
            />
          </div>
        </details>
      )}

      {c.rotation && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>Rotation — vs {c.rotation.benchmark}</SummaryHeading>
          </summary>
          <div style={{ marginTop: 6 }}>
            <FlatTable
              rows={c.rotation.rows}
              rowKey={(r) => r.symbol}
              columns={[
                { key: "symbol", label: "SYM", width: "70px" },
                { key: "group", label: "GROUP", width: "1fr" },
                { key: "ret_1w", label: "1W%", width: "70px" },
                { key: "ret_1m", label: "1M%", width: "70px" },
                { key: "ret_3m", label: "3M%", width: "70px" },
                { key: "ret_ytd", label: "YTD%", width: "70px" },
                { key: "rs_vs_bench_3m", label: "RS 3M", width: "70px" },
              ]}
            />
          </div>
        </details>
      )}

      {c.rrg && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>
              Relative Rotation — vs {c.rrg.benchmark} ({c.rrg.tail_weeks}w tail)
            </SummaryHeading>
          </summary>
          <div style={{ marginTop: 6 }}>
            <FlatTable
              rows={c.rrg.series.map((s) => ({
                symbol: s.symbol,
                group: s.group,
                quadrant: s.quadrant,
                rs_ratio: s.tail.at(-1)?.rs_ratio ?? null,
                rs_momentum: s.tail.at(-1)?.rs_momentum ?? null,
              }))}
              rowKey={(r) => r.symbol}
              columns={[
                { key: "symbol", label: "SYM", width: "70px" },
                { key: "group", label: "GROUP", width: "1fr" },
                { key: "quadrant", label: "QUADRANT", width: "110px" },
                { key: "rs_ratio", label: "RS RATIO", width: "80px" },
                { key: "rs_momentum", label: "RS MOM", width: "80px" },
              ]}
            />
          </div>
        </details>
      )}

      {c.volatility && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>Volatility — term {c.volatility.term_state}</SummaryHeading>
          </summary>
          <div style={{ marginTop: 6 }}>
            <ArithmeticGrid data={humanizeGridFields(c.volatility as unknown as Record<string, unknown>)} exclude={["vix_history"]} />
          </div>
        </details>
      )}

      {c.breadth && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>
              Breadth — {c.breadth.primary_market}
              {c.breadth.warming_up && <span style={{ color: theme.warn }}> · warming up (&lt;40 sessions)</span>}
            </SummaryHeading>
          </summary>
          <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 6 }}>
            <FlatTable
              rows={c.breadth.markets}
              rowKey={(r: BreadthMarket) => r.market}
              columns={[
                { key: "market", label: "MARKET", width: "1fr" },
                { key: "breadth_pct", label: "BREADTH%", width: "80px" },
                { key: "ad_ratio", label: "A/D", width: "70px" },
                { key: "net_highs", label: "NET HI/LO", width: "80px" },
                { key: "trin", label: "TRIN", width: "70px" },
              ]}
            />
            <ArithmeticGrid data={humanizeGridFields(c.breadth as unknown as Record<string, unknown>)} exclude={["markets", "ad_line", "mcclellan_history"]} />
          </div>
        </details>
      )}

      {c.sentiment && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            {/* P2 fix: routed through the shared `qty()` formatter (1dp) —
                was a raw "64.971429" float. */}
            <SummaryHeading level={5}>
              Sentiment — Fear &amp; Greed {qty(c.sentiment.fg_score, 1)} ({c.sentiment.fg_rating ?? "n/a"})
            </SummaryHeading>
          </summary>
          <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 6 }}>
            {c.sentiment.cot.length > 0 && (
              <FlatTable
                rows={c.sentiment.cot}
                rowKey={(r: CotRow) => r.code}
                columns={[
                  { key: "label", label: "SERIES", width: "1.4fr" },
                  { key: "net", label: "NET", width: "90px" },
                  { key: "pct_oi", label: "% OI", width: "70px" },
                  { key: "percentile", label: "PCTL", width: "70px" },
                  { key: "change_1w", label: "1W CHG", width: "80px" },
                ]}
              />
            )}
          </div>
        </details>
      )}

      {c.valuation && c.valuation.metrics.length > 0 && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>Valuation (CAPE / ERP)</SummaryHeading>
          </summary>
          <div style={{ marginTop: 6 }}>
            <FlatTable
              rows={c.valuation.metrics}
              rowKey={(r: ValuationMetric) => r.key}
              columns={[
                { key: "label", label: "METRIC", width: "1.4fr" },
                { key: "value", label: "VALUE", width: "80px" },
                { key: "percentile", label: "PCTL", width: "70px" },
                { key: "note", label: "NOTE", width: "1.6fr" },
              ]}
            />
          </div>
        </details>
      )}

      {calEnv && (
        <details>
          <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
            <SummaryHeading level={5}>
              Week-Ahead Economic Calendar
              {calEnv.stale && <span style={{ color: theme.warn }}> · stale</span>}
            </SummaryHeading>
          </summary>
          <div style={{ marginTop: 6 }}>
            {calEnv.data.needs_key && <PanelState kind="empty" message="Economic calendar needs an API key for this deployment" />}
            <FlatTable
              rows={calEnv.data.events}
              rowKey={(r, i) => `${r.date ?? "?"}-${r.event}-${i}`}
              columns={[
                { key: "date", label: "DATE", width: "90px" },
                { key: "country", label: "CTRY", width: "60px" },
                { key: "event", label: "EVENT", width: "1.6fr" },
                { key: "actual", label: "ACTUAL", width: "80px" },
                { key: "previous", label: "PRIOR", width: "80px" },
                { key: "consensus", label: "CONSENSUS", width: "80px" },
              ]}
            />
          </div>
        </details>
      )}
    </div>
  );
}

// ── ch4: holdings deep-dive ──────────────────────────────────────────────
function HoldingsBody({ c }: { c: PackHoldingsChapter }) {
  const entries = Object.entries(c.symbols);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      {c.n_symbols_failed > 0 && (
        <div style={{ fontSize: theme.fontSize.micro, color: theme.warn }}>
          {c.n_symbols_failed} symbol{c.n_symbols_failed === 1 ? "" : "s"} failed to build a tear sheet this week (below) — every other held symbol is unaffected.
        </div>
      )}
      {entries.length === 0 ? (
        <PanelState kind="empty" message="No held symbols this week" />
      ) : (
        entries.map(([symbol, entry]) => (
          <details key={symbol} style={{ border: `1px solid ${theme.grid}`, borderRadius: 4 }}>
            <summary className="pack-summary" style={{ padding: "6px 8px", fontSize: theme.fontSize.sm, color: theme.text }}>
              <SummaryHeading level={5}>
                {symbol}
                {/* P3 fix: suppress "BTC — BTC" when the fundamentals name
                    IS the ticker (crypto/ETP symbols with no separate
                    company name). */}
                {!isPackAbsent(entry) && entry.fundamentals.name && entry.fundamentals.name !== symbol && (
                  <span style={{ color: theme.muted }}> — {entry.fundamentals.name}</span>
                )}
              </SummaryHeading>
            </summary>
            <div style={{ padding: "6px 8px", display: "flex", flexDirection: "column", gap: 10 }}>
              {isPackAbsent(entry) ? (
                <PanelState kind="empty" message="Tear sheet unavailable" hint={entry.reason} />
              ) : (
                <>
                  {/* P1 fix (reading layer) + P2 fix (formatting): "last" is
                      this tear sheet's one key figure — promoted to base
                      size, and routed through the shared `qty()` formatter
                      (2dp) rather than fmtValue's 4dp fallback ("1,000.1234"). */}
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 10 }}>
                    <span className="num" style={{ fontSize: theme.fontSize.base, fontWeight: 700, color: theme.text }}>
                      {qty(entry.technicals.last, 2)}
                    </span>
                    <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                      sma50 {qty(entry.technicals.sma50, 2)} · sma200 {qty(entry.technicals.sma200, 2)} · rsi14 {qty(entry.technicals.rsi_14, 1)}
                    </span>
                    <span style={{ fontSize: theme.fontSize.micro, color: entry.technicals.trend === "uptrend" ? theme.up : entry.technicals.trend === "downtrend" ? theme.down : theme.muted }}>
                      {entry.technicals.trend}
                    </span>
                  </div>

                  {/* P2 fix: a forensic score is only ever meaningful for an
                      equity with annual filings — BTC/ETH/GLD/SPY/USDC never
                      score, and rendering a "card" of bare em-dashes for them
                      restates the same house law `ReportView.tsx` already
                      states elsewhere ("REGIME is omitted entirely when
                      null, never a hero-sized bare dash"). Require a real
                      score before a card renders at all. */}
                  {(() => {
                    const gradableCards = entry.forensic.cards.filter((card) => card.score !== null);
                    if (gradableCards.length === 0) return null;
                    return (
                      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>FORENSIC SCORES</div>
                        {gradableCards.map((card) => {
                          const delta = entry.forensic.deltas[card.key];
                          return (
                            <div key={card.key} style={{ fontSize: theme.fontSize.micro, color: theme.text }}>
                              <span className="num">{card.label}: {card.score}</span>
                              {card.verdict && <span style={{ color: theme.muted }}> ({card.verdict})</span>}
                              {delta && delta.delta !== null && (
                                <span className="num" style={{ color: delta.delta >= 0 ? theme.up : theme.down }}> {delta.delta >= 0 ? "+" : ""}{delta.delta} — {delta.label}</span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })()}

                  {/* Fix round 3 (small item), then fix round 4 (P0
                      regression, this round): same non-null gate as the
                      forensic scores above — BTC/ETH/GLD/SPY/USDC have no
                      annual filings, so every margin line was rendering as
                      "Gross/Operating/Net Margin % (latest FY): —" (5 bare
                      em-dashes). The round-3 fix itself crashed the whole
                      tree: `values` can be `[]` (not just a same-length
                      array of `null`s) for a symbol with zero fiscal years
                      of margin history, so `values[0]` is `undefined` at
                      RUNTIME — `undefined !== null` is `true`, so the
                      `!== null` filter let it straight through, and the
                      `!` non-null assertion after it is a compile-time-only
                      claim with no runtime check, so `undefined.toFixed`
                      threw with no error boundary above it. Fixed by
                      extracting the value ONCE per line and gating on it
                      with `!= null` (the LOOSE comparison — catches
                      `undefined` AND `null` in one check, unlike `!==`)
                      before ever calling a method on it; no `!` assertion
                      anywhere in this block now. Audited the rest of this
                      file for the same shape (an `!== null`/`!` pair guarding
                      a value that could ALSO be `undefined`, not just
                      `null`): `s.tail.at(-1)?.rs_ratio` (RRG) already uses
                      optional chaining, not indexing + assertion — safe.
                      `entry.forensic.deltas[card.key]` is guarded by a plain
                      truthy check (`delta &&`), which excludes `undefined`
                      too — safe. `tools[tool]!` in the shadow verdicts list
                      sits behind `tools[tool] && (...)`, a truthy guard on
                      the EXACT SAME expression it later asserts — safe.
                      `c.regime.score`/`cm.value`/`card.score` are direct
                      Pydantic-model fields (always serialized as `value` or
                      JSON `null`, never simply absent), not array/record
                      indexing that can silently miss — left as `!== null`,
                      not the same risk class as an array index. */}
                  {(() => {
                    const gradableMargins: { key: string; label: string; v: number }[] = [];
                    for (const line of entry.fundamentals.margins) {
                      const v = line.values[0];
                      if (v != null) gradableMargins.push({ key: line.key, label: line.label, v });
                    }
                    if (gradableMargins.length === 0) return null;
                    return (
                      <div style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px" }}>
                        {gradableMargins.map(({ key, label, v }) => (
                          <span key={key} className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                            {label} (latest FY): {v.toFixed(2)}%
                          </span>
                        ))}
                      </div>
                    );
                  })()}

                  {entry.valuation.ratios.length > 0 && (
                    <FlatTable
                      rows={entry.valuation.ratios}
                      rowKey={(r) => r.key}
                      columns={[
                        { key: "label", label: "RATIO", width: "1.4fr" },
                        { key: "value", label: "VALUE", width: "80px" },
                        { key: "unit", label: "UNIT", width: "60px" },
                      ]}
                    />
                  )}

                  {(() => {
                    const patterns: ScanPattern[] = entry.scan.rows.flatMap((r) => r.patterns);
                    return patterns.length > 0 ? (
                      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>PATTERN SCAN</div>
                        {patterns.map((p, i) => (
                          <div key={i} style={{ fontSize: theme.fontSize.micro, color: p.bias > 0 ? theme.up : p.bias < 0 ? theme.down : theme.muted }}>
                            {p.label} ({p.bias > 0 ? "+" : ""}{p.bias}) — {p.detail}
                          </div>
                        ))}
                      </div>
                    ) : null;
                  })()}

                  {entry.news.length > 0 && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>NEWS</div>
                      {entry.news.slice(0, 10).map((n, i) => (
                        // Plain text nodes only — n.title/n.summary are untrusted,
                        // web-sourced strings; React escapes text children, and
                        // this never uses dangerouslySetInnerHTML.
                        <div key={i} style={{ fontSize: theme.fontSize.micro, color: theme.text }}>
                          {n.title} <span style={{ color: theme.muted }}>— {n.source ?? "unknown source"}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </details>
        ))
      )}
    </div>
  );
}

// ── ch5: quant ────────────────────────────────────────────────────────────
// P1 fix: explicit column order for the FACTOR STUDY grid — identity fields
// first, then statistics — instead of `ArithmeticGrid`'s natural object-key
// order interleaving NAME/FACTOR/WINDOW START with IC IR/IC MEAN/IC TSTAT at
// equal weight. Built by copying only these keys, in this order, into a
// fresh object — `ArithmeticGrid` itself stays the same generic renderer
// (`Object.entries()` iterates in insertion order), no new grid component.
const FACTOR_IDENTITY_KEYS: (keyof FactorReport)[] = ["name", "factor", "horizon_days", "window_start", "window_end", "n_periods", "n_symbols", "n_obs"];
const FACTOR_STAT_KEYS: (keyof FactorReport)[] = ["ic_mean", "ic_std", "ic_ir", "ic_tstat", "ic_hit_rate", "long_short_spread"];
function orderedFactorData(f: PackFactorPayload): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of FACTOR_IDENTITY_KEYS) out[k] = f[k];
  for (const k of FACTOR_STAT_KEYS) out[k] = f[k];
  return out;
}

function QuantBody({ c }: { c: PackQuantChapter }) {
  const crossSections = Object.entries(c.cross_section);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />

      {isPackAbsent(c.factor) ? (
        <PanelState kind="empty" message="No factor study for this report week" hint={c.factor.reason} />
      ) : (
        <div>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", marginBottom: 4 }}>FACTOR STUDY</div>
          <ArithmeticGrid data={humanizeGridFields(orderedFactorData(c.factor))} />
          {c.factor.stale && (
            <div style={{ fontSize: theme.fontSize.micro, color: theme.warn, marginTop: 4 }}>
              stale — shipped from report {c.factor.stale_source_report_week}: {c.factor.stale_reason}
            </div>
          )}
          {/* P1 fix: resolution failures as a plain sentence — was a raw
              `JSON.stringify` blob via `fmtValue`'s object fallback
              (`{"USDC":"junk/stablecoin (risk_skip_set) — not resolvable to
              a price series"}`). */}
          {c.factor.symbol_resolution_failures && Object.keys(c.factor.symbol_resolution_failures).length > 0 && (
            <div style={{ fontSize: theme.fontSize.micro, color: theme.warn, marginTop: 4 }}>
              {c.factor.n_symbols_resolution_failed ?? Object.keys(c.factor.symbol_resolution_failures).length} symbol
              {(c.factor.n_symbols_resolution_failed ?? Object.keys(c.factor.symbol_resolution_failures).length) === 1 ? "" : "s"} could not be
              resolved to a price series for this study: {Object.entries(c.factor.symbol_resolution_failures).map(([sym, reason]) => `${sym} (${reason})`).join("; ")}
            </div>
          )}
        </div>
      )}

      {c.scoreboard.rows.length > 0 && (
        <div>
          {/* P1 fix (reading layer): "N of M signals significant" is this
              chapter's one key figure — promoted to the document hero size. */}
          {(() => {
            const nSignificant = c.scoreboard.rows.filter((r) => r.significant).length;
            return (
              <div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: nSignificant > 0 ? theme.up : theme.muted, marginBottom: 4 }}>
                {nSignificant} of {c.scoreboard.rows.length} signal{c.scoreboard.rows.length === 1 ? "" : "s"} significant
              </div>
            );
          })()}
          {/* Fix round 5: the significance claim above and its
              caveats must be read at the same glance — `scoreboard.warnings`
              (survivorship/lookahead, small-N, etc.) rendered at BASE size
              right under the hero, never inside a collapsed `<details>` and
              never at micro/muted weight. */}
          {c.scoreboard.warnings.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 2, marginBottom: 6 }}>
              {c.scoreboard.warnings.map((w, i) => (
                <div key={i} style={{ fontSize: theme.fontSize.sm, color: theme.warn }}>
                  {w}
                </div>
              ))}
            </div>
          )}
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", marginBottom: 4 }}>
            SIGNAL SCOREBOARD — horizon {c.scoreboard.horizon_bars} bars, {c.scoreboard.quantiles}q
          </div>
          <FlatTable
            rows={c.scoreboard.rows}
            rowKey={(r) => r.signal_id}
            columns={[
              { key: "signal_id", label: "SIGNAL", width: "1.2fr" },
              { key: "mean_rank_ic", label: "IC", width: "70px" },
              { key: "icir", label: "ICIR", width: "70px" },
              { key: "t_stat", label: "T-STAT", width: "70px" },
              { key: "hit_rate", label: "HIT%", width: "70px", format: (v) => pct1(v as number | null) },
              { key: "hit_rate_wilson_lb", label: "WILSON LB", width: "80px", format: (v) => pct1(v as number | null) },
              { key: "significant", label: "SIG?", width: "60px" },
              { key: "low_confidence", label: "LOW-N?", width: "70px" },
            ]}
          />
        </div>
      )}

      {crossSections.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>
            CROSS-SECTION (held symbols only — within-book ordering, not an independent factor reading)
          </div>
          {crossSections.map(([signalId, cs]) => {
            const keys = Array.from(new Set(cs.rows.flatMap((r) => Object.keys(r))));
            const cols: { key: keyof PackQuantCrossSectionRow; label: string }[] = keys.map((k) => ({ key: k, label: k.toUpperCase() }));
            return (
              <details key={signalId}>
                <summary className="pack-summary" style={{ fontSize: theme.fontSize.sm, color: theme.text, padding: "4px 2px" }}>
                  <SummaryHeading level={5}>
                    {signalId} — as of {cs.as_of_date} ({cs.n_universe} in full universe)
                  </SummaryHeading>
                </summary>
                <div style={{ marginTop: 6 }}>
                  <FlatTable rows={cs.rows} rowKey={(r, i) => `${r.symbol}-${i}`} columns={cols} />
                </div>
              </details>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── ch6: shadow-mode tool grading — prominence: the
// evidence-only nature label, benchmark column, Wilson LB and k-deflation
// note are surfaced directly in the table, never behind a further click. ──
const VERDICT_COLOR: Record<string, string> = {
  add_watch: theme.up,
  trim_watch: theme.down,
  flag: theme.warn,
  hold: theme.muted,
};

// Fix round 3 (small item): the real warning string is
// `shadow.py::_score_tool`'s own literal — "only N week(s) of
// shadow_verdicts history (< M) — indicative only" — a programmer plural
// and a raw table name, both fine in a backend log, neither fine in
// reader-facing prose. Reformats the SAME two real numbers rather than
// inventing new ones; falls back to the raw string untouched if the shape
// ever changes upstream (never silently drops the information).
function humanizeLowHistoryWarning(w: string): string {
  const m = w.match(/only (\d+) week\(s\) of shadow_verdicts history \(< (\d+)\)/);
  if (!m) return w;
  const weeks = Number(m[1]);
  const min = Number(m[2]);
  return `only ${weeks} week${weeks === 1 ? "" : "s"} of verdict history (need ${min}) — indicative only`;
}

function ShadowBody({ c }: { c: PackShadowChapter }) {
  const toolEntries = Object.entries(c.tools);
  // P2 fix (box-weight inversion) + P1 fix (reading layer): "K tools graded"
  // is this chapter's actual headline and is now promoted above the
  // definition box, at hero size. When k is 0, state WHY in the reader's own
  // words rather than a bare "0 tools graded this week" — reusing the real,
  // already-computed per-tool warning string (`shadow.py::_score_tool`'s
  // "only N week(s) of shadow_verdicts history (< M) — indicative only",
  // humanized above) rather than inventing a new client-side figure.
  const lowHistoryWarning = toolEntries.flatMap(([, t]) => t.warnings ?? []).find((w) => w.includes("week(s) of shadow_verdicts history"));
  const kLabel =
    c.k === undefined
      ? null
      : c.k === 0
        ? `0 tools graded yet${lowHistoryWarning ? ` — ${humanizeLowHistoryWarning(lowHistoryWarning)}` : c.grade_min_weeks !== undefined ? ` — needs ${c.grade_min_weeks} weeks of verdict history` : ""}`
        : `${c.k} of ${toolEntries.length} tool${toolEntries.length === 1 ? "" : "s"} graded this week`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />

      {kLabel && (
        <div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: c.k === 0 ? theme.muted : theme.text }}>
          {kLabel}
        </div>
      )}

      {/* Evidence-only label — still its own block (never
          a muted footnote), now a DEFINITION (neutral border) rather than a
          caution (was amber) — the headline above carries the actual news. */}
      <div style={{ fontSize: theme.fontSize.base, color: theme.text, fontFamily: theme.fontSerif, border: `1px solid ${theme.border}`, borderRadius: 4, padding: "6px 10px" }}>
        {c.nature}
      </div>

      {c.grade_min_weeks !== undefined && c.k !== 0 && (
        <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          min grading history {c.grade_min_weeks}w
        </div>
      )}
      {c.deflation_note && <div style={{ fontSize: theme.fontSize.micro, color: theme.warn }}>{c.deflation_note}</div>}

      {toolEntries.length === 0 ? (
        <PanelState kind="empty" message="No shadow tools recorded for this week" />
      ) : (
        (() => {
          // Fix round 5 (additive shadow-owner fields, coded defensively —
          // not yet in `shadow.py` as of this pass, must render cleanly with
          // or without them): an EFFECTIVE N column only when at least one
          // tool actually carries it, never a column of bare dashes.
          const hasEffectiveN = toolEntries.some(([, t]) => t.effective_n !== undefined);
          const template = hasEffectiveN ? "110px 80px 90px 90px 80px 70px 60px 1fr" : "110px 80px 90px 90px 70px 60px 1fr";
          return (
            <>
              {/* `priced_as_of` surfaced near the table caption, not buried
                  in a row — table-level, not per-tool. Trimmed to seconds
                  (same `humanizeGridFields` ISO convention as the VaR grid's
                  `as_of`) — was a raw microsecond+offset timestamp. */}
              <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>
                SCOREBOARD
                {c.priced_as_of && (
                  <>
                    {" "}
                    — priced as of {ISO_TIMESTAMP_RE.test(c.priced_as_of) ? c.priced_as_of.slice(0, 19).replace("T", " ") : c.priced_as_of}
                  </>
                )}
              </div>
              <div style={{ border: `1px solid ${theme.grid}`, borderRadius: 4, overflowX: "auto" }}>
                <div style={{ display: "grid", gridTemplateColumns: template, gap: 14, padding: "2px 8px", fontSize: theme.fontSize.micro, color: theme.muted, borderBottom: `1px solid ${theme.grid}` }}>
                  <span>TOOL</span>
                  <span style={{ textAlign: "right" }}>HIT RATE</span>
                  <span style={{ textAlign: "right" }}>BENCHMARK</span>
                  <span style={{ textAlign: "right" }}>WILSON LB</span>
                  {hasEffectiveN && <span style={{ textAlign: "right" }}>EFFECTIVE N</span>}
                  <span style={{ textAlign: "right" }}>N GRADED</span>
                  <span style={{ textAlign: "right" }}>EVENTS</span>
                  <span>NOTE</span>
                </div>
                {toolEntries.map(([tool, t]: [string, ShadowToolGrade]) => (
                  <div key={tool} style={{ display: "grid", gridTemplateColumns: template, gap: 14, padding: "2px 8px", fontSize: theme.fontSize.micro, borderBottom: `1px solid ${theme.grid}` }}>
                    <span style={{ color: theme.text, textTransform: "uppercase" }}>{tool}</span>
                    <span className="num" style={{ textAlign: "right", color: t.hit_rate == null ? theme.muted : theme.text }}>{pct1(t.hit_rate)}</span>
                    <span className="num" style={{ textAlign: "right", color: theme.muted }}>{pct1(t.benchmark_hit_rate)}</span>
                    <span className="num" style={{ textAlign: "right", color: theme.muted }}>{pct1(t.hit_rate_wilson_lb)}</span>
                    {/* Same null-glyph rule as N GRADED: "—" iff not graded,
                        a real number (0 included, `null` collapsed to 0)
                        iff it is — was mixing 0 and "—" via a bare `?? "—"`
                        that didn't check `graded` first. */}
                    {hasEffectiveN && <span className="num" style={{ textAlign: "right", color: theme.muted }}>{t.graded ? (t.effective_n ?? 0) : "—"}</span>}
                    {/* P3 fix: N GRADED's null glyph is now unambiguous — "—"
                        iff the tool isn't graded at all, a real number (0
                        included) iff it is. */}
                    <span className="num" style={{ textAlign: "right", color: theme.muted }}>{t.graded ? (t.n_graded ?? 0) : "—"}</span>
                    <span className="num" style={{ textAlign: "right", color: theme.muted }}>{t.n_events ?? "—"}</span>
                    {/* Fix round 5: NOTE is now a short status word only —
                        the FULL warning text (overlap-veto, one-week
                        history) moved to its own readable block below the
                        table, never squeezed micro-sized into a table cell. */}
                    <span style={{ color: t.graded ? (t.low_confidence ? theme.warn : theme.muted) : theme.warn }}>
                      {t.graded ? (t.low_confidence ? "low confidence" : "graded") : t.reason ?? "not graded"}
                    </span>
                  </div>
                ))}
              </div>
            </>
          );
        })()
      )}

      {/* Fix round 5: every per-tool warning — overlap-veto,
          one-week/short-history — rendered here at readable size (never
          micro-muted, never behind a `<details>`), visible whenever ANY
          tool carries one, not just when the chapter-level headline is
          k===0. `pushes` (additive, shadow owner, optional) gets the same
          treatment when present and nonzero — a real graded outcome that
          was neither a hit nor a miss. */}
      {(() => {
        const lines: { tool: string; text: string }[] = [];
        for (const [tool, t] of toolEntries) {
          for (const w of t.warnings ?? []) lines.push({ tool, text: humanizeLowHistoryWarning(w) });
          if (t.pushes !== undefined && t.pushes > 0) {
            lines.push({ tool, text: `${t.pushes} push${t.pushes === 1 ? "" : "es"} (graded, neither a hit nor a miss)` });
          }
        }
        if (lines.length === 0) return null;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {lines.map(({ tool, text }, i) => (
              <div key={i} style={{ fontSize: theme.fontSize.sm, color: theme.warn }}>
                <span className="num" style={{ textTransform: "uppercase", color: theme.text }}>{tool}</span>: {text}
              </div>
            ))}
          </div>
        );
      })()}

      {c.verdicts && Object.keys(c.verdicts).length > 0 && (
        <div>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", marginBottom: 4 }}>
            CURRENT VERDICTS (demo mode — no graded history yet)
          </div>
          {Object.entries(c.verdicts).map(([symbol, tools]) => (
            <div key={symbol} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, padding: "2px 0", fontSize: theme.fontSize.micro, borderBottom: `1px solid ${theme.grid}` }}>
              <span className="num" style={{ width: 70, color: theme.text }}>{symbol}</span>
              {(["technicals", "patterns", "forensic"] as const).map(
                (tool) =>
                  tools[tool] && (
                    <span key={tool} title={tools[tool]!.rationale} style={{ color: VERDICT_COLOR[tools[tool]!.verdict] ?? theme.muted }}>
                      {tool}: {tools[tool]!.verdict}
                    </span>
                  ),
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── forecasts — the ledger's own issuance summary for
// this pack build (`report_worker.py::_issue_forecasts`), not the full
// ledger (that's the standalone `/forecasts` router — Phase 19 UI). ───────
function ForecastsBody({ c }: { c: PackForecastsChapter }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      {/* P2: the pending count used to sit as
          an unframed 11px muted span buried among five other captions below
          — but the report-wide nudge that LINKS here ("N forecasts pending
          → ") promises pending as the headline, and this chapter opened on
          "issued" instead. State both, equally weighted, and frame pending
          with its own left rule so it reads as its own fact, not a caption
          in a list. Placement (still the chapter's opening line) upheld. */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: theme.text }}>
          {c.issued} issued
        </span>
        <span
          className="num"
          style={{
            fontSize: theme.fontSize.sm,
            fontWeight: 700,
            color: c.pending_total > 0 ? theme.accent : theme.muted,
            borderLeft: `2px solid ${theme.border}`,
            paddingLeft: 10,
          }}
        >
          {c.pending_total} pending{c.next_due ? ` · next due ${c.next_due}` : ""}
        </span>
      </div>
      <div className="num" style={{ display: "flex", flexWrap: "wrap", gap: "2px 16px", fontSize: theme.fontSize.micro, color: theme.muted }}>
        <span>{c.duplicates} duplicate{c.duplicates === 1 ? "" : "s"}</span>
        <span>{c.skipped.covered} already covered</span>
        <span>{c.skipped.ungradeable} ungradeable</span>
      </div>
      {c.issuance_errors.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.warn, letterSpacing: "0.04em" }}>ISSUANCE ERRORS</div>
          {c.issuance_errors.map((e, i) => (
            <div key={i} style={{ fontSize: theme.fontSize.micro, color: theme.warn }}>
              {e.issued_by ?? "?"} {e.subject ?? ""}: {e.reason}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── calibration — grades the forecast ledger (Brier +
// discrimination), one card per issuer×horizon. `bins===null` (machine
// issuers, B2) renders `bins_note` in the same serif caption register
// `EngineCaption` uses; a human issuer's real reliability bins render as a
// table, n=0 rows dimmed rather than dropped (m13's empty-bin honesty). ───
function CalibrationGroupCard({ g }: { g: CalibrationGroup }) {
  const s = g.summary;
  const skillColor = g.low_confidence || s.brier_skill == null ? theme.muted : s.brier_skill > 0 ? theme.up : s.brier_skill < 0 ? theme.down : theme.muted;
  return (
    <details style={{ border: `1px solid ${theme.grid}`, borderRadius: 4 }}>
      <summary className="pack-summary" style={{ padding: "6px 8px", fontSize: theme.fontSize.sm, color: theme.text }}>
        <SummaryHeading level={5}>
          {g.issued_by} — {g.horizon_days}d
          {g.low_confidence && <span style={{ color: theme.warn }}> · low confidence</span>}
        </SummaryHeading>
      </summary>
      <div style={{ padding: "6px 8px", display: "flex", flexDirection: "column", gap: 8 }}>
        <div className="num" style={{ display: "flex", flexWrap: "wrap", gap: "3px 16px", fontSize: theme.fontSize.micro }}>
          <span style={{ color: theme.text }}>brier {s.brier_mean == null ? "—" : s.brier_mean.toFixed(4)}</span>
          <span style={{ color: theme.muted }}>ref (p̄) {s.brier_ref == null ? "—" : s.brier_ref.toFixed(4)}</span>
          <span style={{ color: theme.muted }}>coin {s.brier_coin.toFixed(2)}</span>
          <span style={{ color: skillColor }}>skill {s.brier_skill == null ? "—" : s.brier_skill.toFixed(3)}</span>
          <span style={{ color: theme.text }}>hit {pct1(s.hit_rate)}</span>
          <span style={{ color: theme.muted }}>benchmark {pct1(s.benchmark_hit_rate)}</span>
          <span style={{ color: theme.muted }}>wilson lb {pct1(s.wilson_lb)}</span>
          <span style={{ color: theme.muted }}>n {s.n} (eff {s.effective_n})</span>
          <span style={{ color: theme.muted }}>pushes {s.pushes}</span>
          <span style={{ color: theme.muted }}>abstained {s.abstained} ({pct1(s.abstention_rate)})</span>
          <span style={{ color: theme.muted }}>pending {g.pending}</span>
        </div>
        {g.deflation_note && <div style={{ fontSize: theme.fontSize.micro, color: theme.warn }}>{g.deflation_note}</div>}
        {g.bins === null ? (
          <div style={{ fontFamily: theme.fontSerif, fontSize: theme.fontSize.micro, color: theme.muted }}>{g.bins_note}</div>
        ) : (
          <div style={{ border: `1px solid ${theme.grid}`, borderRadius: 4, overflowX: "auto" }}>
            <div style={{ display: "grid", gridTemplateColumns: "90px 60px 80px 80px 90px", gap: 8, padding: "2px 8px", fontSize: theme.fontSize.micro, color: theme.muted, borderBottom: `1px solid ${theme.grid}` }}>
              <span>BIN</span>
              <span style={{ textAlign: "right" }}>N</span>
              <span style={{ textAlign: "right" }}>MEAN P</span>
              <span style={{ textAlign: "right" }}>HIT RATE</span>
              <span style={{ textAlign: "right" }}>WILSON LB</span>
            </div>
            {g.bins.map((b) => (
              // n=0 bins are DIMMED, never hidden — a
              // sparse issuer's histogram stays honest about coverage. Dimming via `opacity` on top
              // of cells that are ALREADY `theme.muted` text compounded to
              // ~2.3:1 contrast — dim via the row's own background instead,
              // leaving every cell's text at its normal, passing contrast.
              <div key={b.bin} style={{ display: "grid", gridTemplateColumns: "90px 60px 80px 80px 90px", gap: 8, padding: "2px 8px", fontSize: theme.fontSize.micro, borderBottom: `1px solid ${theme.grid}`, background: b.n === 0 ? theme.panelHeader : "transparent" }}>
                <span className="num" style={{ color: theme.text }}>{b.bin}</span>
                <span className="num" style={{ textAlign: "right", color: theme.muted }}>{b.n}</span>
                <span className="num" style={{ textAlign: "right", color: theme.muted }}>{b.mean_p == null ? "—" : b.mean_p.toFixed(2)}</span>
                <span className="num" style={{ textAlign: "right", color: theme.muted }}>{pct1(b.hit_rate)}</span>
                <span className="num" style={{ textAlign: "right", color: theme.muted }}>{pct1(b.wilson_lb)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

function CalibrationBody({ c }: { c: PackCalibrationChapter }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      {c.groups.length === 0 ? (
        <PanelState kind="empty" message="No forecast groups have resolved outcomes yet" />
      ) : (
        c.groups.map((g) => <CalibrationGroupCard key={`${g.issued_by}-${g.horizon_days}`} g={g} />)
      )}
    </div>
  );
}

// ── decisions — one card per CURRENT leaf recommendation:
// funding source, projected CVaR delta, invalidation condition, review/
// expiry dates, lifecycle chain (collapsed). Every field renders from the
// REAL tagged-union branch the backend service docstring documents — never
// a generic JSON dump. ──────────────────────────────────────────────────
function fundingSourceLine(f: DecisionFundingSource): string {
  switch (f.kind) {
    case "none":
      return f.reason;
    case "clearing_cash":
      return `${eur(f.amount_eur)} from ${f.account} (available ${eur(f.available_eur)}${f.covers ? "" : " — insufficient"})`;
    case "proceeds":
      return `${eur(f.est_proceeds_eur)} proceeds from ${f.symbols.join(", ") || "n/a"} → ${f.to} (${f.lots})`;
    case "unknown":
      return f.reason;
    default:
      return fmtValue(f);
  }
}

// P2: the colour direction here is CORRECT
// domain semantics (a negative CVaR delta is a risk REDUCTION — good, green
// — even though the general numeric convention elsewhere in this document
// is "minus is red") but relying on hue ALONE to carry that meaning is an
// anti-pattern regardless of which direction is "right" — a reader primed
// by the rest of the app's red-for-negative convention can misread a green
// minus here. Direction upheld (the colour law is unchanged); the fix is
// spelling the meaning out in the text itself so it never depends on hue.
function riskDeltaLine(r: DecisionRiskBudgetImpact): { text: string; color: string } {
  if ("unavailable" in r) return { text: r.unavailable, color: theme.muted };
  const color = r.delta_pct > 0 ? theme.down : r.delta_pct < 0 ? theme.up : theme.muted;
  const meaning = r.delta_pct > 0 ? ", risk increased" : r.delta_pct < 0 ? ", risk reduced" : ", risk unchanged";
  return {
    text: `CVaR95 ${r.cvar95_now.toFixed(2)}% → ${r.cvar95_post.toFixed(2)}% (${r.delta_pct >= 0 ? "+" : ""}${r.delta_pct.toFixed(2)}pp${meaning})`,
    color,
  };
}

function DecisionCard({ row }: { row: DecisionRow }) {
  const risk = riskDeltaLine(row.risk_budget_impact);
  return (
    <div style={{ border: `1px solid ${theme.grid}`, borderRadius: 4, padding: "8px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: theme.fontSize.sm, fontWeight: 600, color: theme.text }}>{row.rule_id}</span>
        <span className="num" style={{ fontSize: theme.fontSize.micro, color: ACTION_COLOR[row.action] ?? theme.muted, textTransform: "uppercase" }}>
          {row.action}
        </span>
        {row.sizing !== null && <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.text }}>{eur(row.sizing)}</span>}
        <StatusBadge status={row.status} />
      </div>
      {/* P3: six same-weight micro
          lines with nothing to anchor a scan — promote the risk delta (the
          one figure this card already insists on spelling out in text
          rather than leaving to hue alone, see `riskDeltaLine` above) to a
          hero: bigger, bold, its own small-caps label above it — the same
          hero+caption pattern `ForecastsBody`'s "issued"/"pending" figures
          use elsewhere in this file. Every other line on the card stays
          micro; this is the one thing a reader's eye should land on first. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.03em" }}>risk</span>
        <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: risk.color }}>{risk.text}</span>
      </div>
      <div style={{ fontSize: theme.fontSize.micro, color: theme.text }}>
        <span style={{ color: theme.muted, textTransform: "uppercase", letterSpacing: "0.03em" }}>funding: </span>
        {fundingSourceLine(row.funding_source)}
      </div>
      <div style={{ fontSize: theme.fontSize.micro, color: row.invalidation.still_valid === false ? theme.warn : theme.text }}>
        <span style={{ color: theme.muted, textTransform: "uppercase", letterSpacing: "0.03em" }}>invalidation: </span>
        {row.invalidation.text}
        {row.invalidation.still_valid !== null && <span> — {row.invalidation.still_valid ? "still valid" : "invalidated"}</span>}
      </div>
      <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
        review {row.review_at} · expires {row.expires_at}
      </div>
      <details>
        <summary className="pack-summary" style={{ fontSize: theme.fontSize.micro, color: theme.muted, cursor: "pointer" }}>
          lifecycle ({row.lifecycle.length})
        </summary>
        <div style={{ marginTop: 4, display: "flex", flexDirection: "column", gap: 2 }}>
          {row.lifecycle.map((l) => (
            <div key={l.id} className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
              {l.created_at.slice(0, 19).replace("T", " ")} — {l.status}
              {l.human_action ? ` (${l.human_action})` : ""}
              {l.human_note ? `: ${l.human_note}` : ""}
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

function DecisionsBody({ c }: { c: PackDecisionsChapter }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
        no-action rate {pct1(c.no_action_rate)}
        {Object.entries(c.counts).length > 0 && ` · ${Object.entries(c.counts).map(([k, n]) => `${k}: ${n}`).join(" · ")}`}
      </div>
      {c.rows.length === 0 ? (
        <PanelState kind="empty" message="No current decisions this week" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {c.rows.map((row) => (
            <DecisionCard key={row.id} row={row} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── ch7: methodology appendix — per-tool "how to read" callouts ─────────
function MethodologyBody({ c }: { c: PackMethodologyChapter }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <EngineCaption engine={c.engine} howToRead={c.how_to_read} />
      <div className="num" style={{ fontSize: theme.fontSize.base, fontWeight: 700, color: theme.text }}>
        {c.tools.length} tool{c.tools.length === 1 ? "" : "s"} documented
      </div>
      <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>version {c.version}</div>
      {c.tools.map((t: MethodologyTool) => (
        <details key={t.tool} style={{ border: `1px solid ${theme.grid}`, borderRadius: 4 }}>
          <summary className="pack-summary" style={{ padding: "6px 8px", fontSize: theme.fontSize.sm, color: theme.text }}>
            <SummaryHeading level={5}>{t.tool}</SummaryHeading>
          </summary>
          <div style={{ padding: "6px 8px", display: "flex", flexDirection: "column", gap: 4, fontSize: theme.fontSize.micro, color: theme.muted }}>
            {t.measures && <div><span style={{ color: theme.text }}>Measures:</span> {t.measures}</div>}
            {t.endpoint && <div className="num">Endpoint: {t.endpoint}</div>}
            {t.limitations && <div><span style={{ color: theme.text }}>Limitations:</span> {t.limitations}</div>}
            {t.grading_horizon && <div><span style={{ color: theme.text }}>Grading horizon:</span> {t.grading_horizon}</div>}
            {t.status && <div><span style={{ color: theme.text }}>Status:</span> {t.status}</div>}
          </div>
        </details>
      ))}
    </div>
  );
}

// ── the document, in the pinned order:
// exec_summary/portfolio/market/holdings/quant/shadow/methodology), plus
// Phase 18's three additive chapters (forecasts/calibration/decisions)
// slotted after shadow and before methodology per spec. All three are
// OPTIONAL keys (`sections.forecasts?`/`.calibration?`/`.decisions?`) — a
// pack built before this landed, a demo pack (no ledger/projection demo
// seam), or a chapter whose own build failed (calibration/decisions skip
// the key entirely; forecasts ships `{absent:true,reason}` instead) all
// read as "not available", never a crash. ──────────────────────────────
export function ReportPackChapters({ pack }: { pack: ReportPack }) {
  const s = pack.sections;
  const forecastsAbsent = !s.forecasts ? "Not built for this report week" : isPackAbsent(s.forecasts) ? s.forecasts.reason : null;
  const calibrationAbsent = s.calibration ? null : "Not built for this report week";
  const decisionsAbsent = s.decisions ? null : "Not built for this report week";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <style>{DISCLOSURE_CSS}</style>
      <ChapterDetails title="Executive Summary" absentReason={isPackAbsent(s.exec_summary) ? s.exec_summary.reason : null} source={pack.sources.exec_summary} defaultOpen>
        {!isPackAbsent(s.exec_summary) && <ExecSummaryBody c={s.exec_summary} />}
      </ChapterDetails>
      <ChapterDetails title="Portfolio Review" absentReason={isPackAbsent(s.portfolio) ? s.portfolio.reason : null} source={pack.sources.portfolio} defaultOpen>
        {!isPackAbsent(s.portfolio) && <PortfolioBody c={s.portfolio} />}
      </ChapterDetails>
      <ChapterDetails title="Market Context" absentReason={isPackAbsent(s.market) ? s.market.reason : null} source={pack.sources.market}>
        {!isPackAbsent(s.market) && <MarketBody c={s.market} />}
      </ChapterDetails>
      <ChapterDetails title="Holdings Deep-Dive" absentReason={isPackAbsent(s.holdings) ? s.holdings.reason : null} source={pack.sources.holdings}>
        {!isPackAbsent(s.holdings) && <HoldingsBody c={s.holdings} />}
      </ChapterDetails>
      <ChapterDetails title="Quant" absentReason={isPackAbsent(s.quant) ? s.quant.reason : null} source={pack.sources.quant}>
        {!isPackAbsent(s.quant) && <QuantBody c={s.quant} />}
      </ChapterDetails>
      <ChapterDetails title="Shadow-Mode Tool Grading" absentReason={isPackAbsent(s.shadow) ? s.shadow.reason : null} source={pack.sources.shadow} defaultOpen>
        {!isPackAbsent(s.shadow) && <ShadowBody c={s.shadow} />}
      </ChapterDetails>
      <ChapterDetails id="pack-chapter-forecasts" title="Forecasts" absentReason={forecastsAbsent} source={pack.sources.forecasts}>
        {s.forecasts && !isPackAbsent(s.forecasts) && <ForecastsBody c={s.forecasts} />}
      </ChapterDetails>
      <ChapterDetails title="Calibration" absentReason={calibrationAbsent} source={pack.sources.calibration}>
        {s.calibration && <CalibrationBody c={s.calibration} />}
      </ChapterDetails>
      <ChapterDetails title="Decisions" absentReason={decisionsAbsent} source={pack.sources.decisions}>
        {s.decisions && <DecisionsBody c={s.decisions} />}
      </ChapterDetails>
      <ChapterDetails title="Methodology Appendix" absentReason={isPackAbsent(s.methodology) ? s.methodology.reason : null} source={pack.sources.methodology}>
        {!isPackAbsent(s.methodology) && <MethodologyBody c={s.methodology} />}
      </ChapterDetails>
    </div>
  );
}
