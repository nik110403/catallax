"""Demo macro handoff import (DEMO-SPEC section 12): `demo_warm` step.

The private install exports an allowlisted set of `macro:*` Redis values to a JSON
file; this loads it into the demo Redis exactly as `MacroAdapter._cached` would have
stored it (`KEY` with the fresh TTL, `KEY:stale` with 7 d). The file is untrusted
input: size and entry caps, a full-match key allowlist, and pydantic validation of
every value. Bad entries are skipped with a WARNING; nothing here raises on bad data.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.adapters.macro_adapter import (
    CalendarData, IndicatorBoard, MacroAdapter, MacroSeries, RatesData, YieldCurve,
)

logger = logging.getLogger("catallax.macro_import")

MAX_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 500
MAX_AGE = timedelta(hours=48)
MAX_FUTURE = timedelta(hours=1)  # clock-skew allowance; a later generated_at is untrustworthy

KEY_RE = re.compile(r"macro:(yc:(US|EU)|rates:US|indicators|calendar|series:[A-Za-z0-9._]{1,64}:(toya|level))")


def _model_for(key: str) -> type[BaseModel]:
    if key.startswith("macro:yc:"):
        return YieldCurve
    if key == "macro:rates:US":
        return RatesData
    if key == "macro:indicators":
        return IndicatorBoard
    if key == "macro:calendar":
        return CalendarData
    return MacroSeries


def _is_empty(data: BaseModel) -> bool:
    return not (getattr(data, "points", None) or getattr(data, "spreads", None)
                or getattr(data, "cells", None) or getattr(data, "events", None))


async def import_macro(redis: Any, ttl: int, path: str | None) -> int:
    """Import the handoff file at `path`; returns the number of keys written.
    Missing path/file -> 0 (INFO). Never raises on bad file content."""
    if not path or not Path(path).is_file():
        logger.info("macro import: no handoff file (%s), skipping", path or "DEMO_MACRO_IMPORT unset")
        return 0
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_BYTES + 1)
        if len(blob) > MAX_BYTES:
            logger.warning("macro import: %s exceeds %d bytes, rejected", path, MAX_BYTES)
            return 0
        doc = json.loads(blob)
    except Exception as exc:  # OSError, ValueError, RecursionError, ...
        logger.warning("macro import: unreadable handoff file: %s", type(exc).__name__)
        return 0
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("entries"), dict):
        logger.warning("macro import: unsupported handoff format (need version 1 with entries), rejected")
        return 0
    entries: dict[str, Any] = doc["entries"]
    if len(entries) > MAX_ENTRIES:
        logger.warning("macro import: %d entries exceeds cap %d, rejected", len(entries), MAX_ENTRIES)
        return 0
    old = False
    try:
        gen = datetime.fromisoformat(str(doc.get("generated_at")).replace("Z", "+00:00"))
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - gen
        if delta > MAX_AGE:
            old = True
            logger.warning("macro import: handoff is stale (generated_at %s, over 48h old), stale copies only", gen.isoformat())
        elif -delta > MAX_FUTURE:
            old = True
            logger.warning("macro import: generated_at %s is in the future, stale copies only", gen.isoformat())
    except ValueError:
        old = True
        logger.warning("macro import: generated_at missing or invalid, age unknown, stale copies only")

    # Keys the exporter could only read from KEY:stale: never promote them to the fresh key.
    raw_stale = doc.get("entries_stale")
    stale_keys = {k for k in raw_stale if isinstance(k, str)} if isinstance(raw_stale, list) else set()

    written = 0
    for key, value in entries.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            logger.warning("macro import: key %r not allowlisted, skipped", str(key)[:80])
            continue
        try:
            raw = value if isinstance(value, str) else json.dumps(value)
            data = _model_for(key).model_validate_json(raw)
        except Exception as exc:
            logger.warning("macro import: %s failed validation, skipped (%s)", key, str(exc)[:120])
            continue
        if _is_empty(data):
            logger.warning("macro import: %s is empty, skipped", key)
            continue
        payload = data.model_dump_json()
        if not old and key not in stale_keys and ttl > 0:  # an old export must show the UI's stale label, never pass as fresh
            await redis.set(key, payload, ex=ttl)
        await redis.set(f"{key}:stale", payload, ex=MacroAdapter._STALE_TTL)
        written += 1
    logger.info("macro import: %d of %d keys imported", written, len(entries))
    return written
