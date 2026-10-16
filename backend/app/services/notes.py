"""Notes / thesis service. `NotesService` is the only writer
of `notes` (Phase 19). A note is a research note or investment
thesis as a first-class object, mirroring roadmap Sec5.3's progressive
disclosure: one sentence -> why it matters -> action -> risk/invalidation ->
long-form body.

**Slug** is the URL id (never a guessable secret, never a capability):
lowercase the title, collapse every run of non-alphanumeric characters to a
single `-`, strip leading/trailing `-`, truncate to 120 chars. A collision
against an existing slug appends `-2`, `-3`, ... Once assigned, the slug is
immutable — `update()` never re-derives it from a later title edit, so a
citation/link into a note keeps working even if the title changes.

**Evidence refs are validated on write** ("cannot assert facts not in the
record" — the same rule the frozen advisor core applies to machine-written
claims, applied here to the owner's own writing): each `{kind, id}` in
`evidence_refs` must resolve to an existing row of that `kind` (`forecast`,
`recommendation`, `journal_entry`, `report_pack`, `shadow_verdict`); `url`
refs are accepted as-is (nothing to resolve) and stored with `kind: "url"`.
An invalid or unresolvable ref raises `InvalidEvidenceRef` (422) naming the
offending ref; other invalid note requests still return 400.

A signed note is immutable except through a new revision: `update()` on a
signed note raises a 409 (no `CatallaxError` subclass carries that status
either; `routers/algo.py`'s `NAME_CONFLICT` 409 is the existing precedent
for a plain `fastapi.HTTPException(status_code=409, detail={...})` where
`errors.py` has no matching domain class — notes' `NOTE_SIGNED` follows
the same shape).

`GET`'s evidence hydration re-resolves every ref against the CURRENT state
of the referenced row (not the label captured at write time, if any) so a
citation always shows live data; a ref whose row was since deleted comes
back `resolved: False` rather than raising — the note itself does not
become unreadable because one citation rotted.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.errors import InvalidEvidenceRef, InvalidRequest, NotFound
from app.models import (
    Forecast,
    JournalEntry,
    Note,
    Recommendation,
    ReportPack,
    ShadowVerdict,
)

_KINDS = frozenset({"research_note", "thesis"})
_RESOLVABLE_EVIDENCE_KINDS = frozenset(
    {"forecast", "recommendation", "journal_entry", "report_pack", "shadow_verdict"}
)
_ALL_EVIDENCE_KINDS = _RESOLVABLE_EVIDENCE_KINDS | {"url"}
_UPDATABLE_FIELDS = frozenset(
    {
        "title",
        "one_sentence",
        "why_it_matters",
        "action",
        "risk_invalidation",
        "body",
        "evidence_refs",
        "subjects",
    }
)
_SLUG_MAX_LEN = 120
_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    slug = _SLUG_COLLAPSE_RE.sub("-", title.strip().lower()).strip("-")
    slug = slug[:_SLUG_MAX_LEN].rstrip("-")
    return slug or "note"


def _note_dict(n: Note) -> dict[str, Any]:
    return {
        "id": n.id,
        "slug": n.slug,
        "kind": n.kind,
        "title": n.title,
        "status": n.status,
        "one_sentence": n.one_sentence,
        "why_it_matters": n.why_it_matters,
        "action": n.action,
        "risk_invalidation": n.risk_invalidation,
        "body": n.body,
        "evidence_refs": n.evidence_refs,
        "subjects": n.subjects,
        "signed_at": n.signed_at.isoformat() if n.signed_at else None,
        "created_at": n.created_at.isoformat(),
        "updated_at": n.updated_at.isoformat(),
    }


async def _fetch_evidence_row(session, kind: str, ref_id: Any) -> Any:
    if kind == "forecast":
        stmt = select(Forecast).where(Forecast.id == ref_id)
    elif kind == "recommendation":
        stmt = select(Recommendation).where(Recommendation.id == ref_id)
    elif kind == "journal_entry":
        stmt = select(JournalEntry).where(JournalEntry.id == ref_id)
    elif kind == "report_pack":
        # ReportPack's natural key IS `report_week` (its PK) — a report_pack
        # evidence ref's generic `id` field holds that week string, not a
        # surrogate integer id.
        stmt = select(ReportPack).where(ReportPack.report_week == ref_id)
    elif kind == "shadow_verdict":
        stmt = select(ShadowVerdict).where(ShadowVerdict.id == ref_id)
    else:
        return None
    return (await session.execute(stmt)).scalar_one_or_none()


def _evidence_label(kind: str, row: Any) -> str | None:
    if kind == "forecast":
        return row.statement
    if kind == "recommendation":
        return f"{row.rule_id}: {row.action}"
    if kind == "journal_entry":
        return row.title
    if kind == "report_pack":
        return f"Report pack {row.report_week}"
    if kind == "shadow_verdict":
        return f"{row.symbol} {row.tool}: {row.verdict}"
    return None


async def _validate_evidence_refs(session, refs: list[dict[str, Any]]) -> None:
    for ref in refs:
        kind = ref.get("kind")
        ref_id = ref.get("id")
        if kind not in _ALL_EVIDENCE_KINDS:
            raise InvalidEvidenceRef(f"evidence ref has unknown kind {kind!r}")
        if kind == "url":
            if not ref_id:
                raise InvalidEvidenceRef(
                    "evidence ref of kind 'url' must include an id (the URL)"
                )
            continue
        row = await _fetch_evidence_row(session, kind, ref_id)
        if row is None:
            raise InvalidEvidenceRef(
                f"evidence ref {kind}:{ref_id!r} does not resolve to an existing row"
            )


async def _hydrate_ref(session, ref: dict[str, Any]) -> dict[str, Any]:
    kind = ref.get("kind")
    ref_id = ref.get("id")
    if kind == "url":
        return {
            "kind": "url",
            "id": ref_id,
            "label": ref.get("label") or ref_id,
            "resolved": True,
        }
    row = await _fetch_evidence_row(session, kind, ref_id)
    if row is None:
        return {
            "kind": kind,
            "id": ref_id,
            "label": ref.get("label"),
            "resolved": False,
        }
    return {
        "kind": kind,
        "id": ref_id,
        "label": _evidence_label(kind, row),
        "resolved": True,
    }


class NotesService:
    """Mirrors `JournalService`'s constructor shape."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── writes ───────────────────────────────────────────────────────────
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind")
        if kind not in _KINDS:
            raise InvalidRequest(f"kind must be one of {sorted(_KINDS)}, got {kind!r}")
        title = (payload.get("title") or "").strip()
        if not title:
            raise InvalidRequest("title must not be empty")
        one_sentence = (payload.get("one_sentence") or "").strip()
        if not one_sentence:
            raise InvalidRequest("one_sentence must not be empty")
        evidence_refs = payload.get("evidence_refs") or []

        async with session_scope() as session:
            await _validate_evidence_refs(session, evidence_refs)
            slug = await self._unique_slug(session, title)
            note = Note(
                slug=slug,
                kind=kind,
                title=title,
                one_sentence=one_sentence,
                why_it_matters=payload.get("why_it_matters"),
                action=payload.get("action"),
                risk_invalidation=payload.get("risk_invalidation"),
                body=payload.get("body"),
                evidence_refs=evidence_refs or None,
                subjects=payload.get("subjects"),
            )
            session.add(note)
            await session.commit()
            await session.refresh(note)
        return {"note": _note_dict(note)}

    async def update(self, slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - _UPDATABLE_FIELDS
        if unknown:
            raise InvalidRequest(
                f"field(s) {sorted(unknown)} are not updatable (allowed: {sorted(_UPDATABLE_FIELDS)})"
            )
        async with session_scope() as session:
            note = (
                await session.execute(
                    select(Note).where(Note.slug == slug).with_for_update()
                )
            ).scalar_one_or_none()
            if note is None:
                raise NotFound(f"note {slug!r} not found")
            if note.status == "signed":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "NOTE_SIGNED",
                        "message": f"note {slug!r} is signed and immutable — create a new revision instead",
                    },
                )
            if "evidence_refs" in payload:
                await _validate_evidence_refs(session, payload["evidence_refs"] or [])
            for key, value in payload.items():
                setattr(note, key, value)
            await session.commit()
            await session.refresh(note)
        return {"note": _note_dict(note)}

    async def sign(self, slug: str) -> dict[str, Any]:
        async with session_scope() as session:
            note = (
                await session.execute(
                    select(Note).where(Note.slug == slug).with_for_update()
                )
            ).scalar_one_or_none()
            if note is None:
                raise NotFound(f"note {slug!r} not found")
            if note.status != "signed":
                note.status = "signed"
                note.signed_at = datetime.now(timezone.utc)
                await session.commit()
                await session.refresh(note)
        return {"note": _note_dict(note)}

    # ── reads ────────────────────────────────────────────────────────────
    async def get(self, slug: str) -> dict[str, Any]:
        async with session_scope() as session:
            note = (
                await session.execute(select(Note).where(Note.slug == slug))
            ).scalar_one_or_none()
            if note is None:
                raise NotFound(f"note {slug!r} not found")
            evidence = [
                await _hydrate_ref(session, ref) for ref in (note.evidence_refs or [])
            ]
        return {"note": _note_dict(note), "evidence": evidence}

    async def list(
        self,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with session_scope() as session:
            stmt = select(Note).order_by(Note.id.desc()).limit(limit)
            if kind is not None:
                stmt = stmt.where(Note.kind == kind)
            if status is not None:
                stmt = stmt.where(Note.status == status)
            rows = (await session.execute(stmt)).scalars().all()
        return [_note_dict(r) for r in rows]

    # ── helpers ──────────────────────────────────────────────────────────
    async def _unique_slug(self, session, title: str) -> str:
        base = _slugify(title)
        slug = base
        n = 1
        while True:
            exists = (
                await session.execute(select(Note.id).where(Note.slug == slug))
            ).scalar_one_or_none()
            if exists is None:
                return slug
            n += 1
            slug = f"{base}-{n}"[:_SLUG_MAX_LEN]
