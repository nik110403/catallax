"""Upstream-mode seam (hosted public demo, DEMO-SPEC E3).

`UPSTREAM_MODE=cache-only` means a cache miss must never trigger an outbound
fetch: the caller serves its stale cached copy, or an empty result marked
stale. Every place that would open an outbound connection calls `guard()`
first (or branches on `is_cache_only()` to serve stale). The warm job
(`app/demo_warm.py`) runs the same code with the mode overridden to `live`.
"""
from __future__ import annotations

from app.config import Settings, get_settings
from app.errors import UpstreamError

CACHE_ONLY = "cache-only"
# Long-lived copy of caches that have no stale key of their own (markets, news).
STALE_SUFFIX = ":stale"
STALE_TTL = 7 * 86400


class UpstreamDisabled(UpstreamError):
    """Raised instead of fetching when the backend runs cache-only."""

    code = "UPSTREAM_DISABLED"
    public_message = "Live upstream fetching is disabled (cache-only demo)"


def is_cache_only(settings: Settings | None = None) -> bool:
    return (settings or get_settings()).upstream_mode == CACHE_ONLY


def guard(what: str, settings: Settings | None = None) -> None:
    """Call immediately before an outbound request; raises in cache-only mode."""
    if is_cache_only(settings):
        raise UpstreamDisabled(f"{what}: upstream fetch skipped (cache-only)")
