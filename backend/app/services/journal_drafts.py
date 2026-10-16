"""Weekly review tick + pack-seeded drafts. Registered as
part of `report_worker._maintenance_tick` — "alongside the existing
maintenance ticks" (`resolve_due`/`expire_and_supersede`), same
scheduling (every worker tick, non-fatal on failure) and the same
`errors: []` health shape as `forecast_resolver.resolve_due`.

**Two responsibilities, one call, gated on "run after the Monday pack is
built":**

1. **Due reviews** — `JournalService.due(before=today)`'s count, computed
   unconditionally (it needs no pack) so the returned/logged dict always
   carries a fresh `journal_due` for the worker's maintenance log line
   (report_worker's "health tick" — there is no dedicated HTTP surface for
   worker health this phase, the log line IS it).
2. **Pack-seeded drafts** — up to `settings.journal_draft_max_per_week`
   `journal_entries` rows (`source="pack_draft"`, `draft_of_week=<week>`,
   `status="open"`, `forecast_id=NULL` — a draft emits nothing), proposed
   from THIS week's already-persisted `shadow_verdicts` (highest
   Laplace-prior conviction first) and any current-leaf `proposed`
   `recommendations` row no journal entry cites yet. Self-gated on the
   current ISO week's `report_packs` row being `status="complete"` — a
   missing/incomplete pack this tick just means zero drafts created (not an
   error); the worker ticks every `REPORT_PACK_TICK_INTERVAL_SECONDS`, so
   the very next tick after the pack lands proposes them, no separate
   scheduling wired for "run right after this build" (report_worker's
   existing per-week advisory lock already prevents this reading a
   half-written pack — `_finalize_complete` commits before any later tick's
   maintenance sweep can observe `status="complete"`).

**Idempotency (natural key):** `(source="pack_draft", draft_of_week,
subject)` — enforced in application logic (no unique index; the schema is
frozen), by reading the week's already-drafted subjects
before proposing new ones and always skipping them. Re-running the tick for
an unchanged week therefore creates no duplicates.

Draft `view` text is explicitly and unmissably fenced as a machine draft —
never presented as the owner's own words (a hard requirement)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.models import Forecast, JournalEntry, Recommendation, ReportPack, ShadowVerdict
from app.services.advisor_report import iso_week
from app.services.forecasts import laplace_probability
from app.services.journal import JournalService

# A journal entry's probability must be strictly between 0 and 1
# (`ck_journal_entries_probability_range`). A shadow-seeded draft derives
# one deterministically (the same Laplace-prior seed `build_shadow_forecasts`
# uses); a recommendation carries no probability of its own to draw from —
# this fixed neutral placeholder ships instead, always overwritten by the owner's
# real estimate before signing (draft SELECTION is deterministic per the
# spec; this one field, on this one candidate kind, has no deterministic
# source and is documented as a placeholder, not a model-generated number).
_RECOMMENDATION_DRAFT_PLACEHOLDER_PROBABILITY = 0.5

_TITLE_MAX_CHARS = 200  # `journal_entries.title` is `String(200)`.


# ── pure helpers (no I/O) ────────────────────────────────────────────────
def _seed_from_scoreboard(tool_stats: dict[str, Any] | None) -> tuple[int, int]:
    """(hits, n) seed from one tool's scoreboard stats — duplicated (not
    imported) from `forecasts._seed_from_scoreboard`, a private helper on a
    module this one treats as read-only; same formula, same shape
    (`sections["shadow"]["tools"][tool]`, i.e. `shadow.py`'s `_score_tool`
    output)."""
    if not tool_stats:
        return 0, 0
    hit_rate = tool_stats.get("hit_rate")
    if hit_rate is None:
        return 0, 0
    n_graded = tool_stats.get("n_graded") or 0
    pushes = tool_stats.get("pushes") or 0
    effective_n = tool_stats.get("effective_n")
    n = effective_n if effective_n is not None else (n_graded - pushes)
    if not n or n <= 0:
        return 0, 0
    return round(hit_rate * n), n


def _shadow_candidates(
    verdict_rows: list[ShadowVerdict], scoreboard_tools: dict[str, Any], report_week: str,
) -> list[dict[str, Any]]:
    """One candidate per SUBJECT (symbol) — the highest-conviction
    (`|probability - 0.5|`) gradeable (`direction IS NOT NULL`; `hold`/
    `forensic` never gradeable, mirrors `build_shadow_forecasts`) verdict
    for that symbol this week. Sorted highest-conviction first, subject
    ascending on a tie — deterministic regardless of DB row order."""
    by_subject: dict[str, dict[str, Any]] = {}
    for v in verdict_rows:
        if v.direction is None:
            continue
        hits, n = _seed_from_scoreboard(scoreboard_tools.get(v.tool))
        probability = laplace_probability(hits, n)
        conviction = abs(probability - 0.5)
        existing = by_subject.get(v.symbol)
        if existing is not None and existing["conviction"] >= conviction:
            continue
        by_subject[v.symbol] = {
            "subject": v.symbol, "tool": v.tool, "verdict": v.verdict, "probability": probability,
            "hits": hits, "n": n, "conviction": conviction,
            "evidence_ref": {"kind": "shadow_verdict", "id": f"{report_week}:{v.tool}:{v.symbol}"},
        }
    return sorted(by_subject.values(), key=lambda c: (-c["conviction"], c["subject"]))


def _recommendation_candidates(
    leaves: list[Recommendation], cited_ids: set[int],
) -> list[dict[str, Any]]:
    """Every current-leaf (`_current_recommendations`) `proposed`
    recommendation not yet cited by any journal entry's `evidence`, sorted
    by id (oldest proposal first — deterministic)."""
    out: list[dict[str, Any]] = []
    for rec in leaves:
        if rec.status != "proposed" or rec.id in cited_ids:
            continue
        subject = (rec.input_snapshot or {}).get("symbol") or rec.rule_id
        out.append({
            "subject": subject, "rec_id": rec.id, "rule_id": rec.rule_id, "action": rec.action,
            "sizing": rec.sizing, "evidence_ref": {"kind": "recommendation", "id": rec.id},
        })
    return sorted(out, key=lambda c: c["rec_id"])


def _shadow_draft(
    candidate: dict[str, Any], report_week: str, entry_date: date, review_date: date, horizon_days: int,
) -> JournalEntry:
    subject, tool, verdict = candidate["subject"], candidate["tool"], candidate["verdict"]
    p, hits, n = candidate["probability"], candidate["hits"], candidate["n"]
    title = f"[Draft] {tool} {verdict} — {subject} ({report_week})"[:_TITLE_MAX_CHARS]
    view = (
        f"[MACHINE DRAFT — not the owner's words] Report Pack {report_week}: the {tool} tool's verdict "
        f"on {subject} this week is '{verdict}'. Laplace-prior probability {p:.2f} from {hits}/{n} "
        "historical grades. Review and edit before signing."
    )
    return JournalEntry(
        entry_date=entry_date, title=title, view=view, probability=p, review_date=review_date,
        horizon_days=horizon_days, subject=subject, source="pack_draft", draft_of_week=report_week,
        status="open", forecast_id=None, evidence=[candidate["evidence_ref"]],
    )


def _recommendation_draft(
    candidate: dict[str, Any], report_week: str, entry_date: date, review_date: date, horizon_days: int,
) -> JournalEntry:
    subject, rule_id, action, sizing = (
        candidate["subject"], candidate["rule_id"], candidate["action"], candidate["sizing"]
    )
    title = f"[Draft] {rule_id} {action} — {subject} ({report_week})"[:_TITLE_MAX_CHARS]
    sizing_txt = f", sizing €{sizing:,.2f}" if sizing is not None else ""
    view = (
        f"[MACHINE DRAFT — not the owner's words] Report Pack {report_week}: recommendation '{rule_id}' "
        f"({action}) is proposed for {subject}{sizing_txt}. No probability estimate available — "
        "a placeholder pending review. Review and edit before signing."
    )
    return JournalEntry(
        entry_date=entry_date, title=title, view=view,
        probability=_RECOMMENDATION_DRAFT_PLACEHOLDER_PROBABILITY, review_date=review_date,
        horizon_days=horizon_days, subject=subject, source="pack_draft", draft_of_week=report_week,
        status="open", forecast_id=None, evidence=[candidate["evidence_ref"]],
    )


# ── the tick ─────────────────────────────────────────────────────────────
async def run_weekly_tick(redis: Redis, settings: Settings, *, now: datetime) -> dict[str, Any]:
    """Returns `{"journal_due", "report_week", "candidates", "drafts_created",
    "errors": []}` (plus `"pack_status"` when the week's pack isn't complete
    yet). Never raises — any DB read here is inside the caller
    (`report_worker._maintenance_tick`)'s own try/except, but this function
    completes the due-review count regardless of pack state, so a caller
    that only wants that number doesn't need to special-case a missing
    pack."""
    report_week = iso_week(now)
    journal_due = len(await JournalService(redis, settings).due(before=now.date()))
    result: dict[str, Any] = {
        "journal_due": journal_due, "report_week": report_week,
        "candidates": 0, "drafts_created": 0, "errors": [],
    }

    async with session_scope() as session:
        pack = (await session.execute(
            select(ReportPack).where(ReportPack.report_week == report_week)
        )).scalar_one_or_none()
        if pack is None or pack.status != "complete":
            result["pack_status"] = pack.status if pack is not None else "missing"
            return result

        existing_draft_subjects = set((await session.execute(
            select(JournalEntry.subject).where(
                JournalEntry.source == "pack_draft", JournalEntry.draft_of_week == report_week,
            )
        )).scalars().all())

        human_forecast_subjects = set((await session.execute(
            select(Forecast.subject).where(
                Forecast.status == "pending", Forecast.issued_by.like("human%"),
            )
        )).scalars().all())

        cited_recommendation_ids: set[int] = set()
        for evidence in (await session.execute(
            select(JournalEntry.evidence).where(JournalEntry.evidence.is_not(None))
        )).scalars().all():
            for ref in evidence or []:
                if isinstance(ref, dict) and ref.get("kind") == "recommendation" and ref.get("id") is not None:
                    cited_recommendation_ids.add(ref["id"])

        verdict_rows = (await session.execute(
            select(ShadowVerdict).where(ShadowVerdict.report_week == report_week)
        )).scalars().all()
        scoreboard_tools = ((pack.sections or {}).get("shadow") or {}).get("tools") or {}
        shadow_candidates = [
            c for c in _shadow_candidates(verdict_rows, scoreboard_tools, report_week)
            if c["subject"] not in human_forecast_subjects and c["subject"] not in existing_draft_subjects
        ]

        # Lazy import — mirrors `decisions.py`'s own precedent for the same
        # symbol, avoiding a module-load-time import edge from a service
        # onto the routers layer.
        from app.routers.reports import _current_recommendations

        all_recommendations = (await session.execute(select(Recommendation))).scalars().all()
        leaves = _current_recommendations(all_recommendations)
        reco_candidates = [
            c for c in _recommendation_candidates(leaves, cited_recommendation_ids)
            if c["subject"] not in existing_draft_subjects
        ]

        result["candidates"] = len(shadow_candidates) + len(reco_candidates)
        remaining = max(0, settings.journal_draft_max_per_week - len(existing_draft_subjects))
        entry_date = now.date()
        review_date = entry_date + timedelta(days=settings.journal_default_horizon_days)
        horizon_days = settings.journal_default_horizon_days

        selected_subjects: set[str] = set()
        created = 0
        for candidate in shadow_candidates + reco_candidates:
            if created >= remaining:
                break
            subject = candidate["subject"]
            if subject in selected_subjects:
                continue
            draft = (
                _shadow_draft(candidate, report_week, entry_date, review_date, horizon_days)
                if "tool" in candidate
                else _recommendation_draft(candidate, report_week, entry_date, review_date, horizon_days)
            )
            session.add(draft)
            selected_subjects.add(subject)
            created += 1

        result["drafts_created"] = created
        await session.commit()

    return result
