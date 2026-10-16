"""Foundations F1: the standard result envelope (OpenBB `OBBject` pattern).

Every adapter/service that serves upstream data can wrap its payload in
`Envelope[T]` so the UI knows *which source* served a figure, *when*, and
whether it is a cache hit or stale. The existing shared `Quote`/`Candle`/
`SymbolMatch` models stay the `data`; this only adds provenance metadata.

Kept deliberately small so Phase 8's `MacroAdapter` and later adapters reuse it
without ceremony. Routers may also surface the same fields as `X-Catallax-*`
response headers when the body shape must stay backward-compatible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Provenance(BaseModel):
    """Where a figure came from and how fresh it is."""

    source: str  # adapter / service name that served the data
    fetched_at: datetime
    cache_hit: bool = False  # served from a cache (Redis / Postgres) without upstream
    stale: bool = False  # served from cache *after* an upstream failure / past freshness
    warnings: list[str] = Field(default_factory=list)


class Envelope(BaseModel, Generic[T]):
    """`data` + provenance. Mirrors OpenBB's `OBBject`."""

    data: T
    source: str
    fetched_at: datetime
    cache_hit: bool = False
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def fresh(cls, data: T, source: str, **kw: object) -> "Envelope[T]":
        return cls(
            data=data,
            source=source,
            fetched_at=datetime.now(timezone.utc),
            **kw,  # type: ignore[arg-type]
        )

    @property
    def provenance(self) -> Provenance:
        return Provenance(
            source=self.source,
            fetched_at=self.fetched_at,
            cache_hit=self.cache_hit,
            stale=self.stale,
            warnings=self.warnings,
        )
