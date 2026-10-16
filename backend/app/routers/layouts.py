"""Layout/workspace persistence router (Foundations F4).

A small key→JSON store backing the dynamic-UI platform. The frontend persists
each UI state bucket (`workspaces`, `presets:<namespace>`) here so a user's tabs
and layout presets follow them across devices, instead of living only in one
browser's localStorage.

Single-user app: no auth, nginx is the only entry point. Defences here are
shape/abuse guards, not access control — keys are allowlisted (so a client can't
write arbitrary columns or oversized keys) and payloads are size-capped (so the
row can't be bloated). Stored blobs are opaque to the backend.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.errors import InvalidRequest, NotFound
from app.models import LayoutRow

router = APIRouter(prefix="/layouts", tags=["layouts"])

# Buckets are "workspaces" / "presets:<namespace>"; the allowlist also permits a
# generated namespace (ns_<base36>). Bound to the column width.
_KEY = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
# Generous ceiling for a layout/workspace JSON blob; rejects accidental or
# malicious bloat well before it reaches the row.
_MAX_BYTES = 256 * 1024


@router.get("")
async def list_layouts(db: AsyncSession = Depends(get_db)) -> list[dict[str, str]]:
    """Stored keys + last-write times (no payloads) — for debugging/sync checks."""
    rows = (await db.execute(select(LayoutRow.key, LayoutRow.updated_at))).all()
    return [{"key": k, "updated_at": u.isoformat()} for k, u in rows]


@router.get("/{key}")
async def get_layout(
    key: str = _KEY, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = (
        await db.execute(select(LayoutRow).where(LayoutRow.key == key))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"No stored state for key '{key}'")
    return {"key": row.key, "data": row.data, "updated_at": row.updated_at.isoformat()}


@router.put("/{key}")
async def put_layout(
    request: Request,
    key: str = _KEY,
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    # Early reject on declared size (defence in depth — the body is already
    # parsed by the time we run, but this still bounds what we persist).
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BYTES:
        raise InvalidRequest("Layout state too large")

    if "data" not in payload:
        raise InvalidRequest("Body must be a JSON object with a 'data' field")
    data = payload["data"]
    if not isinstance(data, (dict, list)):
        raise InvalidRequest("'data' must be a JSON object or array")
    if len(json.dumps(data, separators=(",", ":")).encode("utf-8")) > _MAX_BYTES:
        raise InvalidRequest("Layout state too large")

    stmt = (
        insert(LayoutRow)
        .values(key=key, data=data)
        .on_conflict_do_update(
            constraint="uq_layouts_key",
            set_={"data": data, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
    await db.commit()
    return {"status": "ok", "key": key}
