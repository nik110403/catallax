"""No-op LLM provider — the F3 default. Calls no model.

Echoes the last user message so the seam is exercisable end-to-end in tests and
wiring, while guaranteeing zero outbound model calls until a real provider is
configured at Phase 12. Output is tagged `unverifiable` and warns that it is the
no-op provider, so nothing downstream mistakes an echo for grounded analysis.
"""
from __future__ import annotations

from app.adapters.llm.base import (
    Grounding,
    LLMMessage,
    LLMProviderAdapter,
    LLMResponse,
    Role,
)


class NoOpLLMAdapter(LLMProviderAdapter):
    name = "noop"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        facts: dict[str, object] | None = None,
    ) -> LLMResponse:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == Role.user), ""
        )
        return LLMResponse(
            text=last_user,
            provider=self.name,
            model=None,
            grounding=Grounding.unverifiable,
            warnings=["no-op LLM provider: no model was called"],
        )
