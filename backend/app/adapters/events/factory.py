from __future__ import annotations

from importlib import import_module
from typing import Any

from .base import SourceAdapter, Universe


def build_source_adapters(
    settings: Any, redis: Any, universe: Universe | None = None
) -> list[SourceAdapter]:
    """Build only installed source families; other source modules remain optional seams."""
    adapters: list[SourceAdapter] = []
    for dotted in ("app.adapters.events.rss", "app.adapters.events.edgar"):
        try:
            builder = import_module(dotted).build
        except ModuleNotFoundError as exc:
            if exc.name == dotted:
                continue
            raise
        adapters.extend(builder(settings, redis, universe))
    return adapters
