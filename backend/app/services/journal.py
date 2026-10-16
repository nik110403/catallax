"""Analyst journal service. `JournalService` is the only
writer of `journal_entries`; it never writes `forecasts`/`outcomes`
directly — every entry that emits a forecast does so through the existing
`ForecastLedger`, the ledger's own PIT/probability/floor
validation still applies to a journal-issued row.

**Central rule: creating an entry emits a forecast in the same transaction
path.** `create()` opens ONE `session_scope()`, INSERTs the entry (flushed,
not committed, so its `id` is available for `evidence_refs` without leaking
to other sessions), issues the forecast through `ledger.issue()` (its own,
separate transaction — `ForecastLedger.issue()` commits internally on
success), then links `entry.forecast_id` and commits the entry session
LAST. If the ledger rejects the row, the exception propagates out of the
`async with` block before that final commit — the entry session is closed
without ever committing, so Postgres rolls back the flushed INSERT and no
orphan `journal_entries` row is left behind (mirrors `ForecastLedger`'s own
never-orphan guarantee for `outcomes`).

Note on the spec's literal pseudocode: it shows `ledger.issue_human
({"issued_by": "human:journal", ...})`, but `ForecastLedger.issue_human`
(Phase 18, frozen this phase — outside this module's scope) hardcodes
`issued_by="human"` on the row it builds, ignoring any `issued_by` the
caller passes. Since the spec's own "Done when" requires `GET
/api/v1/forecasts?issued_by=human:journal` to show the new row pending,
this service instead builds the row itself (mirroring `issue_human`'s
shape exactly, `issued_by="human:journal"`) and calls the lower-level,
already-general `ledger.issue([row])` — still "the existing ledger", still
never a direct `forecasts` INSERT, just not through the one convenience
method that can't express a non-"human" issuer without modifying
`services/forecasts.py`, which this module does not own.

Every journal-issued forecast's `target` is `{"kind": "manual", "statement":
<title>}` regardless of `resolution_source` — a quick-entry probability is
a claim about a "view", not a directional price target the
`daily_bars` resolver can grade (`forward_return`/`comparator`/
`threshold`), and the spec gives journal entries no `direction` field to
build one from. `resolution_source` still follows the Phase-18 contract
(`daily_bars:<source>` when `subject` matches a known `daily_bars` symbol,
else `manual`) so the metadata is honest about coverage; `forecast_resolver
.resolve_due` already skips anything whose `resolution_source` doesn't
start with `"daily_bars:"` and wraps each row in a per-forecast try/except,
so a `daily_bars:`-sourced journal forecast with a manual-shaped target
degrades to a logged per-tick resolver error, never a crash — a later phase's
concern to enrich, not this module's to guess at.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.errors import InvalidRequest, NotFound
from app.models import DailyBarRow, Forecast, JournalEntry
from app.services.forecasts import ForecastLedger

_JOURNAL_ISSUER = "human:journal"
_JOURNAL_ISSUER_VERSION = "journal-entry-v1"
# Progressive-disclosure text fields — the ONLY
# fields `update()` accepts. `probability`/`subject`/`entry_date`/
# `horizon_days` are frozen the moment a forecast is emitted (they ARE the
# forecast's natural key + probability); `title`/`view` are the claim text
# itself — the spec is explicit that changing a view means a new,
# superseding entry, not an edit to this one.
_PROSE_FIELDS = frozenset({"thesis", "evidence", "invalidation", "sizing_note", "confidence_note"})


def _entry_dict(e: JournalEntry) -> dict[str, Any]:
    return {
        "id": e.id, "entry_date": e.entry_date.isoformat(), "title": e.title, "view": e.view,
        "probability": e.probability, "review_date": e.review_date.isoformat(),
        "horizon_days": e.horizon_days, "subject": e.subject, "thesis": e.thesis,
        "evidence": e.evidence, "invalidation": e.invalidation, "sizing_note": e.sizing_note,
        "confidence_note": e.confidence_note, "status": e.status, "source": e.source,
        "draft_of_week": e.draft_of_week, "forecast_id": e.forecast_id,
        "reviewed_at": e.reviewed_at.isoformat() if e.reviewed_at else None,
        "review_note": e.review_note,
        "created_at": e.created_at.isoformat(), "updated_at": e.updated_at.isoformat(),
    }


def _forecast_summary(f: Forecast) -> dict[str, Any]:
    """Lean projection for `get()`'s joined read — the full 20-field
    `Forecast` row is `/api/v1/forecasts/{id}`'s job, not this service's."""
    return {
        "id": f.id, "issued_by": f.issued_by, "subject": f.subject, "statement": f.statement,
        "probability": f.probability, "decision_date": f.decision_date.isoformat(),
        "due_date": f.due_date.isoformat(), "status": f.status,
        "resolution_source": f.resolution_source,
    }


class JournalService:
    """Mirrors `ForecastLedger`'s constructor shape."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── writes ───────────────────────────────────────────────────────────
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = (payload.get("title") or "").strip()
        view = (payload.get("view") or "").strip()
        if not title:
            raise InvalidRequest("title must not be empty")
        if not view:
            raise InvalidRequest("view must not be empty")
        probability = payload.get("probability")
        if probability is None or not (0.0 < probability < 1.0):
            raise InvalidRequest("probability must be strictly between 0 and 1")
        subject = payload["subject"]
        entry_date = payload.get("entry_date") or date.today()
        review_date = payload["review_date"]
        if review_date <= entry_date:
            raise InvalidRequest("review_date must be after entry_date")
        horizon_days = (review_date - entry_date).days
        lo, hi = self._settings.journal_min_horizon_days, self._settings.journal_max_horizon_days
        if not (lo <= horizon_days <= hi):
            raise InvalidRequest(f"horizon_days={horizon_days} outside [{lo}, {hi}]")

        ledger = ForecastLedger(self._redis, self._settings)
        # Natural-key idempotency check BEFORE the entry insert: a repeat
        # submission at the same (subject, entry_date, horizon) links the
        # already-issued forecast instead of attempting a second `issue()`
        # (which `uq_forecasts_issuer_subject_date_horizon` would only
        # silently no-op anyway — checking first keeps the entry->forecast
        # link explicit rather than relying on that side effect).
        existing = await ledger.get_by_natural_key(
            issued_by=_JOURNAL_ISSUER, subject=subject, decision_date=entry_date, horizon_days=horizon_days,
        )

        async with session_scope() as session:
            entry = JournalEntry(
                entry_date=entry_date, title=title, view=view, probability=probability,
                review_date=review_date, horizon_days=horizon_days, subject=subject,
                thesis=payload.get("thesis"), evidence=payload.get("evidence"),
                invalidation=payload.get("invalidation"), sizing_note=payload.get("sizing_note"),
                confidence_note=payload.get("confidence_note"),
            )
            session.add(entry)
            await session.flush()  # populates entry.id; NOT committed yet

            if existing is not None:
                forecast = existing
            else:
                source = await self._known_symbol_source(subject)
                now = datetime.now(timezone.utc)
                resolution_source = f"daily_bars:{source}" if source else "manual"
                resolution_rule: dict[str, Any] = (
                    {"symbol": subject, "source": source} if source else {"manual": True}
                )
                row = {
                    "issued_by": _JOURNAL_ISSUER, "issuer_version": _JOURNAL_ISSUER_VERSION,
                    "subject_kind": "symbol" if source else "macro", "subject": subject,
                    "statement": title, "kind": "binary", "probability": probability, "direction": None,
                    "target": {"kind": "manual", "statement": title},
                    "horizon_days": horizon_days, "decision_date": entry_date, "due_date": review_date,
                    "report_week": None, "published_at": now, "first_seen_at": now, "available_at": now,
                    "resolution_source": resolution_source, "resolution_rule": resolution_rule,
                    "evidence_refs": [{"kind": "journal_entry", "id": entry.id}],
                }
                result = await ledger.issue([row])
                if result["errors"]:
                    # Propagates out of the `async with` block WITHOUT a
                    # commit — the flushed entry INSERT above is rolled
                    # back on session close. No orphan entry.
                    raise InvalidRequest(f"forecast rejected: {result['errors'][0]['reason']}")
                forecast = await ledger.get_by_natural_key(
                    issued_by=_JOURNAL_ISSUER, subject=subject, decision_date=entry_date,
                    horizon_days=horizon_days,
                )
                if forecast is None:  # pragma: no cover — issue() just reported 1 issued
                    raise InvalidRequest("forecast issuance did not land — natural key not found after issue")

            entry.forecast_id = forecast["id"]
            await session.commit()
            await session.refresh(entry)

        return {
            "entry": _entry_dict(entry),
            "forecast": {
                "id": forecast["id"], "statement": forecast["statement"],
                "probability": forecast["probability"], "due_date": forecast["due_date"],
            },
        }

    async def update(self, entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - _PROSE_FIELDS
        if unknown:
            raise InvalidRequest(
                f"field(s) {sorted(unknown)} are immutable after emission — prose fields only "
                f"({sorted(_PROSE_FIELDS)})"
            )
        async with session_scope() as session:
            entry = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == entry_id).with_for_update()
            )).scalar_one_or_none()
            if entry is None:
                raise NotFound(f"journal entry {entry_id} not found")
            for key, value in payload.items():
                setattr(entry, key, value)
            await session.commit()
            await session.refresh(entry)
        return {"entry": _entry_dict(entry)}

    async def review(self, entry_id: int, note: str, outcome_hint: str | None = None) -> dict[str, Any]:
        status = outcome_hint or "reviewed"
        if status not in ("reviewed", "superseded"):
            raise InvalidRequest(f"invalid review status {status!r} — must be 'reviewed' or 'superseded'")
        async with session_scope() as session:
            entry = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == entry_id).with_for_update()
            )).scalar_one_or_none()
            if entry is None:
                raise NotFound(f"journal entry {entry_id} not found")
            entry.status = status
            entry.reviewed_at = datetime.now(timezone.utc)
            entry.review_note = note
            await session.commit()
            await session.refresh(entry)
        return {"entry": _entry_dict(entry)}

    async def supersede(self, entry_id: int, new_entry_id: int) -> dict[str, Any]:
        """No dedicated successor-link column exists on `journal_entries`
        — the successor id is recorded in `review_note` (the
        only free-text field available besides `evidence`), appended to any
        existing note rather than clobbering it."""
        async with session_scope() as session:
            entry = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == entry_id).with_for_update()
            )).scalar_one_or_none()
            if entry is None:
                raise NotFound(f"journal entry {entry_id} not found")
            marker = f"superseded_by:{new_entry_id}"
            entry.review_note = f"{entry.review_note}\n{marker}" if entry.review_note else marker
            entry.status = "superseded"
            entry.reviewed_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(entry)
        return {"entry": _entry_dict(entry)}

    # ── reads ────────────────────────────────────────────────────────────
    async def get(self, entry_id: int) -> dict[str, Any]:
        async with session_scope() as session:
            entry = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == entry_id)
            )).scalar_one_or_none()
            if entry is None:
                raise NotFound(f"journal entry {entry_id} not found")
            forecast_row = None
            if entry.forecast_id is not None:
                forecast_row = (await session.execute(
                    select(Forecast).where(Forecast.id == entry.forecast_id)
                )).scalar_one_or_none()
        return {
            "entry": _entry_dict(entry),
            "forecast": _forecast_summary(forecast_row) if forecast_row is not None else None,
        }

    async def list(
        self,
        status: str | None = None,
        subject: str | None = None,
        due_before: date | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with session_scope() as session:
            stmt = select(JournalEntry).order_by(JournalEntry.id.desc()).limit(limit)
            if status is not None:
                stmt = stmt.where(JournalEntry.status == status)
            if subject is not None:
                stmt = stmt.where(JournalEntry.subject == subject)
            if due_before is not None:
                stmt = stmt.where(JournalEntry.review_date <= due_before)
            rows = (await session.execute(stmt)).scalars().all()
        return [_entry_dict(r) for r in rows]

    async def due(self, before: date, limit: int = 100) -> list[dict[str, Any]]:
        return await self.list(status="open", due_before=before, limit=limit)

    # ── helpers ──────────────────────────────────────────────────────────
    async def _known_symbol_source(self, subject: str) -> str | None:
        """`settings.price_source_default` when `subject` matches a
        `daily_bars` symbol (any source — this only asks "is this a priced
        symbol at all", not which adapter last wrote it), else `None`."""
        async with session_scope() as session:
            row = (await session.execute(
                select(DailyBarRow.symbol).where(DailyBarRow.symbol == subject).limit(1)
            )).scalar_one_or_none()
        return self._settings.price_source_default if row is not None else None
