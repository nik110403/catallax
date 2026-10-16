"""LLM provider registry + `get_llm_adapter` dependency.

Selected by `LLM_PROVIDER` (default `noop`). Only the no-op provider is
registered now; Phase 12 adds Ollama/cloud providers here without touching
routers. Mirrors the price-adapter factory.
"""
from __future__ import annotations

from fastapi import Depends

from app.adapters.llm.base import LLMProviderAdapter
from app.adapters.llm.claude_code import ClaudeCodeAdapter
from app.adapters.llm.noop import NoOpLLMAdapter
from app.config import Settings, get_settings
from app.errors import AdapterNotFound

LLM_PROVIDERS: dict[str, type[LLMProviderAdapter]] = {
    "noop": NoOpLLMAdapter,
    "claude-code": ClaudeCodeAdapter,
}


def build_llm_adapter(name: str, settings: Settings) -> LLMProviderAdapter:
    cls = LLM_PROVIDERS.get(name)
    if cls is None:
        raise AdapterNotFound(
            f"Unknown LLM provider '{name}'. Known: {', '.join(sorted(LLM_PROVIDERS))}"
        )
    return cls(settings)


def get_llm_adapter(
    settings: Settings = Depends(get_settings),
) -> LLMProviderAdapter:
    return build_llm_adapter(settings.llm_provider, settings)
