"""LLM adapter contract.

The seam exists so later phases can narrate computed numbers with a model. Two
rules are baked into the types, not left to convention:

1. **Deterministic compute is separate from the LLM.** Scores, grades, returns,
   risk metrics are pure Python and reproducible; the model only *narrates*
   numbers already computed — it never produces the numbers.
2. **Every claim is fact-grounded.** Narrative output tags each claim
   `grounded` (traceable to a provided figure), `speculative` (model inference),
   or `unverifiable`. Consumers may surface or filter by tag.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel, Field

from app.config import Settings


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


class Grounding(str, Enum):
    grounded = "grounded"  # traceable to a figure passed in `facts`
    speculative = "speculative"  # model inference beyond the given facts
    unverifiable = "unverifiable"  # cannot be checked


class LLMMessage(BaseModel):
    role: Role
    content: str


class LLMResponse(BaseModel):
    # `text` is UNTRUSTED model output (the no-op default even echoes the user's
    # own message). Any consumer rendering it must HTML-escape at the edge —
    # never inject it as raw markup.
    text: str
    provider: str
    model: str | None = None
    # Structured payload when the model returned a JSON OBJECT (the full parsed dict).
    # Narration callers ignore this and use `text`; structured callers (e.g. news
    # enrichment) read the fields they need from here. UNTRUSTED like `text`.
    data: dict[str, object] | None = None
    grounding: Grounding = Grounding.unverifiable
    warnings: list[str] = Field(default_factory=list)


class LLMProviderAdapter(ABC):
    """Base for all LLM providers. Concrete providers (Ollama, cloud) land in
    Phase 12; the no-op default ships now so the seam is testable with zero
    runtime model calls."""

    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        facts: dict[str, object] | None = None,
    ) -> LLMResponse:
        """Produce a narrative response. `facts` are the deterministic,
        Python-computed figures the model may narrate (and only those)."""

    async def health(self) -> bool:
        """Whether the provider is reachable/usable. No-op default is always up."""
        return True
