"""Event-source contracts and staged adapter factory."""

from .base import PollResult, RawEvent, SourceAdapter, Universe
from .factory import build_source_adapters

__all__ = [
    "PollResult",
    "RawEvent",
    "SourceAdapter",
    "Universe",
    "build_source_adapters",
]
