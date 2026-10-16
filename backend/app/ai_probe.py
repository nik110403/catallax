"""Phase-12 hard-gate proof: run one real `claude -p` through the seam in-container.

`python -m app.ai_probe` in the ai-runner container. Confirms the binary is on PATH,
the mounted creds authenticate, and the grounded-JSON parse works — BEFORE the intel
pipeline is built on top. Prints the LLMResponse and exits non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import sys

from app.adapters.llm.base import LLMMessage, Role
from app.adapters.llm.claude_code import ClaudeCodeAdapter
from app.config import get_settings


async def main() -> int:
    a = ClaudeCodeAdapter(get_settings())
    if not await a.health():
        print("HEALTH FAIL: claude binary not runnable / not authed", file=sys.stderr)
        return 2
    resp = await a.complete(
        [LLMMessage(role=Role.user, content="Give a one-sentence tone read of this headline.")],
        facts={"title": "ACME Corp reports record quarterly revenue", "source": "Reuters"},
    )
    print(json.dumps(resp.model_dump(), indent=2))
    if resp.warnings:
        print("WARNINGS:", resp.warnings, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
