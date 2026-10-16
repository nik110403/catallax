"""LLM provider seam (Foundations F3).

Mirrors `PriceSourceAdapter`: an ABC + factory so a real provider can be plugged
in at Phase 12 without touching routers. The default is a no-op so nothing calls
a model yet. See `skills/llm-adapters.md` for the fact-grounding contract.
"""
