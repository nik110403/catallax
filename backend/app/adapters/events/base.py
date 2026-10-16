from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class Universe:
    symbols: frozenset[str] = frozenset()
    cik_map: Mapping[str, str] = field(default_factory=dict)
    refs: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)
    macro_forecast_ids: tuple[int, ...] = ()


@dataclass
class RawEvent:
    source: str
    feed: str
    source_key: str
    event_type: str
    form_type: str | None
    title: str
    summary: str | None
    url: str
    published_at: datetime | None
    first_seen_at: datetime
    raw: dict[str, Any]
    entity_hints: list[tuple[str, str, str]]


@dataclass
class PollResult:
    events: list[RawEvent]
    cursor: dict[str, Any]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


class SourceAdapter(ABC):
    source: str
    feed: str
    rate_limit_per_min: int

    @abstractmethod
    async def poll(
        self, cursor: dict[str, Any], etag: str | None, last_modified: str | None
    ) -> PollResult:
        """Fetch one response. Adapters own HTTP; callers own persistence."""
