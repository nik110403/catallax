"""Notes / thesis routes. All under `/api/v1/notes`. The
share-gate is global middleware (`ShareGateMiddleware`, wraps the whole app
in `main.py`) — there is no per-route dependency to add here, same as
`routers/journal.py`.

DEMO_MODE (mirrors `routers/journal.py`): the list-shaped GET (`GET /`)
returns an empty, zero-count envelope — never a DB read, never a
`NotesService` construction. The single-item GET (`GET /{slug}`) has no
demo fixture to show, so it 404s instead of returning a shape that would
misleadingly imply "no such note" vs "no demo data at all" — the same
choice `routers/journal.py`'s single-item GET makes. Every mutation (`POST
/`, `PATCH /{slug}`, `POST /{slug}/sign`, `POST /{slug}/export`) raises
`DemoModeReadOnly` (403) before touching anything.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.auth import is_authenticated
from app.errors import DemoModeReadOnly, NotFound
from app.redis import get_redis
from app.routers.networth import get_wealthfolio
from app.services.note_pdf import render_note_pdf
from app.services.notes import NotesService
from app.services.privacy_scan import redact, scan
from app.services.report_pdf import resolve_pdf_path
from app.services.wealthfolio import WealthfolioBridge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notes", tags=["notes"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 1000


class NoteCreateRequest(BaseModel):
    kind: str = Field(pattern="^(research_note|thesis)$")
    title: str = Field(min_length=1, max_length=200)
    one_sentence: str = Field(min_length=1)
    why_it_matters: str | None = None
    action: str | None = None
    risk_invalidation: str | None = None
    body: str | None = None
    evidence_refs: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    subjects: list[str] | None = Field(default=None, max_length=100)


class NoteUpdateRequest(BaseModel):
    """`extra="forbid"` so an attempt to PATCH a server-managed field
    (`kind`, `slug`, `status`, `signed_at`) 422s at the API boundary;
    `NotesService.update` also rejects them for callers that go straight to
    the service."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    one_sentence: str | None = None
    why_it_matters: str | None = None
    action: str | None = None
    risk_invalidation: str | None = None
    body: str | None = None
    evidence_refs: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    subjects: list[str] | None = Field(default=None, max_length=100)


@router.post("/", status_code=201)
async def create_note(
    body: NoteCreateRequest,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await NotesService(redis, settings).create(body.model_dump())


@router.get("/")
async def list_notes(
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_empty_reads:
        return {"notes": [], "count": 0}
    rows = await NotesService(redis, settings).list(
        kind=kind, status=status, limit=limit
    )
    return {"notes": rows, "count": len(rows)}


@router.get("/{slug}")
async def get_note(
    slug: str,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_empty_reads:
        raise NotFound("notes are not available in demo mode")
    return await NotesService(redis, settings).get(slug)


@router.patch("/{slug}")
async def update_note(
    body: NoteUpdateRequest,
    slug: str,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    payload = body.model_dump(exclude_unset=True)
    return await NotesService(redis, settings).update(slug, payload)


@router.post("/{slug}/sign")
async def sign_note(
    slug: str,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await NotesService(redis, settings).sign(slug)


def _export_note(
    payload: dict[str, Any],
    settings: Settings,
    redacted: bool,
    account_labels: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    note = dict(payload["note"])
    note["evidence"] = [dict(ref) for ref in payload.get("evidence", [])]
    counts: Counter[str] = Counter()
    # Only fields actually printed in note.html; retain identifiers and all
    # other persisted fields untouched (no mutation or retention side effects).
    for key in (
        "title",
        "kind",
        "status",
        "one_sentence",
        "why_it_matters",
        "action",
        "risk_invalidation",
        "body",
    ):
        value = note.get(key)
        if isinstance(value, str):
            cleaned, result = (
                redact(value, settings, extra_labels=account_labels)
                if redacted
                else (value, scan(value, settings, extra_labels=account_labels))
            )
            note[key] = cleaned
            counts.update(hit["rule"] for hit in result["findings"])
    for ref in note["evidence"]:
        for key in ("kind", "id", "label"):
            if isinstance(ref.get(key), str):
                cleaned, result = (
                    redact(ref[key], settings, extra_labels=account_labels)
                    if redacted
                    else (
                        ref[key],
                        scan(ref[key], settings, extra_labels=account_labels),
                    )
                )
                ref[key] = cleaned
                counts.update(hit["rule"] for hit in result["findings"])
    if redacted and settings.notes_privacy_scan_strict:
        # Strict rescan catches residual sensitive text across field boundaries
        # or partially matched amounts before any PDF bytes are written.
        for value in [
            *(
                note.get(k)
                for k in (
                    "title",
                    "kind",
                    "status",
                    "one_sentence",
                    "why_it_matters",
                    "action",
                    "risk_invalidation",
                    "body",
                )
            ),
            *(ref.get(k) for ref in note["evidence"] for k in ("kind", "id", "label")),
        ]:
            if isinstance(value, str):
                residual = scan(value, settings, extra_labels=account_labels)[
                    "findings"
                ]
                if residual:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "PRIVACY_RESIDUAL",
                            "rule": residual[0]["rule"],
                            "offset": residual[0]["offset"],
                        },
                    )
    note["scan_counts"] = dict(sorted(counts.items())) if redacted else {}
    return note, {"clean": not counts, "rules": dict(sorted(counts.items()))}


async def _export(
    slug: str,
    redacted: bool,
    request: Request,
    settings: Settings,
    redis: Redis,
    wealthfolio: WealthfolioBridge,
) -> dict[str, Any]:
    if settings.demo_mode:
        raise NotFound("notes are not available in demo mode")
    if not redacted and (
        not settings.share_passphrase.get_secret_value()
        or not is_authenticated(request, settings)
    ):
        raise HTTPException(
            status_code=403,
            detail="unredacted export requires the share gate and an authenticated session",
        )
    account_labels: tuple[str, ...] = ()
    try:
        holdings = await wealthfolio.holdings()
        account_labels = tuple(
            a.account for a in holdings.by_account if a.account.strip()
        )
    except Exception as exc:
        if redacted:
            raise HTTPException(
                status_code=503,
                detail={"code": "PRIVACY_LABELS_UNAVAILABLE"},
            ) from exc
        logger.warning(
            "Wealthfolio account labels unavailable for unredacted note export %r: %s",
            slug,
            exc,
        )
    if redacted and not account_labels:
        # Wealthfolio answered but named zero accounts — an empty label set
        # is indistinguishable from "labels silently failed to load" and
        # must fail closed exactly like the exception path above, never
        # proceed as if there were simply nothing to redact.
        raise HTTPException(
            status_code=503,
            detail={"code": "PRIVACY_LABELS_UNAVAILABLE"},
        )
    payload = await NotesService(redis, settings).get(slug)
    note, summary = _export_note(payload, settings, redacted, account_labels)
    base = settings.notes_export_dir
    path = await render_note_pdf(note, redacted, base)
    path = resolve_pdf_path(path, base)
    if not os.path.isfile(path) or path == os.path.realpath(base):
        raise NotFound("export PDF not available")
    return {
        "path": path,
        "filename": Path(path).name,
        "redacted": redacted,
        "scan": summary,
    }


@router.post("/{slug}/export")
async def export_note(
    request: Request,
    slug: str = ApiPath(..., min_length=1, max_length=120),
    redacted: bool = True,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    wealthfolio: WealthfolioBridge = Depends(get_wealthfolio),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await _export(slug, redacted, request, settings, redis, wealthfolio)


@router.get("/{slug}/export.pdf")
async def download_note_pdf(
    request: Request,
    slug: str = ApiPath(..., min_length=1, max_length=120),
    redacted: bool = True,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    wealthfolio: WealthfolioBridge = Depends(get_wealthfolio),
) -> FileResponse:
    result = await _export(slug, redacted, request, settings, redis, wealthfolio)
    return FileResponse(
        result["path"], media_type="application/pdf", filename=result["filename"]
    )
