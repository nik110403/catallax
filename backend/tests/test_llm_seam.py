"""F3: the LLM seam exists and works with the no-op provider — zero model calls."""
from __future__ import annotations

import pytest

from app.adapters.llm.base import Grounding, LLMMessage, LLMProviderAdapter, Role
from app.adapters.llm.factory import build_llm_adapter
from app.adapters.llm.noop import NoOpLLMAdapter
from app.config import Settings
from app.errors import AdapterNotFound


def _settings(**kw: object) -> Settings:
    # Only the secrets lack defaults; everything else (incl. llm_provider) defaults.
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **kw,
    )


async def test_default_provider_is_noop() -> None:
    s = _settings()
    adapter = build_llm_adapter(s.llm_provider, s)
    assert isinstance(adapter, NoOpLLMAdapter)
    assert isinstance(adapter, LLMProviderAdapter)


async def test_noop_echoes_last_user_and_tags_unverifiable() -> None:
    adapter = build_llm_adapter("noop", _settings())
    resp = await adapter.complete(
        [
            LLMMessage(role=Role.system, content="you narrate numbers"),
            LLMMessage(role=Role.user, content="net worth is 12,345"),
        ]
    )
    assert resp.text == "net worth is 12,345"
    assert resp.provider == "noop"
    assert resp.model is None
    # No-op output must never read as grounded analysis.
    assert resp.grounding is Grounding.unverifiable
    assert resp.warnings  # explicitly flags that no model was called


async def test_unknown_provider_raises() -> None:
    with pytest.raises(AdapterNotFound):
        build_llm_adapter("gpt-9000", _settings(llm_provider="gpt-9000"))


async def test_noop_health_is_up() -> None:
    assert await NoOpLLMAdapter(_settings()).health() is True
