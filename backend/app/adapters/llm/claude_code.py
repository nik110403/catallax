"""Claude Code LLM provider.

Runs the headless `claude -p ... --output-format json` CLI as an async subprocess.
Lives behind the F3 `LLMProviderAdapter` seam so a route/service never calls it
directly and Ollama can replace it later with zero router changes.

Only ever invoked inside the off-API `ai-runner` worker (the sole container with
the `claude` binary + mounted CLI credentials). The API container keeps the
no-op provider. Numbers are computed in Python and handed in via `facts=`; the
model only narrates them, and its text is returned UNTRUSTED (escaped at render).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from app.adapters.llm.base import (
    Grounding,
    LLMMessage,
    LLMProviderAdapter,
    LLMResponse,
    Role,
)

logger = logging.getLogger("catallax.llm.claude_code")

# Security review follow-up (medium): a nonzero-exit excerpt is persisted to
# `report_packs` and rendered in UI/PDF unredacted — a CLI failure mode that
# echoes a credential (e.g. a leaked API key in an error message) would
# otherwise store/display it. Known secret shapes only; not a general
# secret-scanner.
_SECRET_PATTERN = re.compile(
    r"sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+|oat01-[A-Za-z0-9_-]+"
)
# Security review follow-up (low, CWE-117 log forging): control chars/newlines
# in the excerpt could forge extra log lines or break single-line rendering.
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")

_SYSTEM = (
    "You narrate pre-computed financial facts. You MUST NOT invent numbers: every "
    "figure you mention must appear in the FACTS block. Reply with ONLY a JSON object "
    '{"text": <string>, "grounding": <"grounded"|"speculative"|"unverifiable">}. '
    "Use \"grounded\" if every claim traces to a FACTS value; \"speculative\" if you "
    "infer beyond them (tone, outlook); \"unverifiable\" if you cannot tell. No prose "
    "outside the JSON."
)


class ClaudeCodeAdapter(LLMProviderAdapter):
    name = "claude-code"

    def _build_prompt(self, messages: list[LLMMessage], facts: dict[str, object] | None) -> str:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == Role.user), ""
        )
        facts_block = json.dumps(facts or {}, ensure_ascii=False, default=str)
        return (
            f"{_SYSTEM}\n\nFACTS:\n{facts_block}\n\nTASK:\n{last_user}"
        )

    @staticmethod
    def _parse_cli_json(stdout: str) -> tuple[str, str | None]:
        """Parse `claude -p --output-format json` → (result_text, model).

        The CLI wraps the model reply in an envelope whose `result` is the model's
        raw string output. We return that string as-is (the caller extracts the
        inner {text,grounding} JSON)."""
        env = json.loads(stdout)
        result = env.get("result", "")
        model = env.get("model")
        if not isinstance(result, str):
            result = json.dumps(result)
        return result, model

    async def _run_cli(self, prompt: str) -> tuple[int, str, str]:
        """Spawn `claude -p` and return (returncode, stdout, stderr). Times out per settings."""
        s = self._settings
        proc = await asyncio.create_subprocess_exec(
            s.claude_code_bin, "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(s.claude_code_max_turns),
            "--allowedTools", "",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",  # scratch cwd — no repo/project access
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=s.claude_code_timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
            raise TimeoutError("claude -p timed out")
        except BaseException:
            # review 225d5f4 re-review F5: a caller-side cancel (e.g. the
            # report-worker's hard `asyncio.timeout` around enrichment, or
            # narration's own wait_for) surfaces here as CancelledError, not
            # TimeoutError — the branch above never caught it, so the `claude
            # -p` child kept running orphaned past its own cancel. Kill +
            # reap (shielded: cleanup must finish even though this coroutine
            # is already being cancelled) for ANY exit besides the timeout
            # above, then re-raise unchanged.
            proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
            raise
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    @staticmethod
    def _sanitize_excerpt(text: str) -> str:
        """Redact known secret shapes, then collapse control chars/newlines
        to a single space — applied to the FULL string before truncation
        (redaction only shortens text, so the length cap below stays
        accurate) so a secret split across the truncation boundary can't
        survive as a partial match either."""
        text = _SECRET_PATTERN.sub("[redacted]", text)
        return _CONTROL_CHARS_PATTERN.sub(" ", text)

    @classmethod
    def _nonzero_exit_excerpt(cls, stdout: str, stderr: str) -> str:
        """Bounded diagnostic text for a nonzero-exit warning (failure mode:
        the CLI can transiently exit 1 with EMPTY stderr but a JSON error
        envelope on stdout, e.g. a revoked/expired OAuth token — the stderr
        tail alone was a useless "exit 1: "). Prefers stdout's parsed JSON
        `result` field, then a raw stdout tail, then the stderr tail as
        before — whichever is the first to actually have content. Sanitized
        (`_sanitize_excerpt`) since this text is persisted to `report_packs`
        and rendered in UI/PDF, not just logged."""
        if stdout.strip():
            try:
                env = json.loads(stdout)
            except (ValueError, TypeError):
                env = None
            if isinstance(env, dict) and isinstance(env.get("result"), str) and env["result"].strip():
                return cls._sanitize_excerpt(env["result"])[:200]
            return cls._sanitize_excerpt(stdout)[:200]
        return cls._sanitize_excerpt(stderr)[:120]

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        facts: dict[str, object] | None = None,
    ) -> LLMResponse:
        prompt = self._build_prompt(messages, facts)
        try:
            code, stdout, stderr = await self._run_cli(prompt)
        except TimeoutError:
            return LLMResponse(text="", provider=self.name, grounding=Grounding.unverifiable,
                               warnings=["claude-code: invocation timed out"])
        except FileNotFoundError:
            return LLMResponse(text="", provider=self.name, grounding=Grounding.unverifiable,
                               warnings=["claude-code: binary not found (is the ai-runner image built?)"])
        if code != 0:
            excerpt = self._nonzero_exit_excerpt(stdout, stderr)
            logger.warning("claude-code exited %d: %s", code, excerpt)
            return LLMResponse(text="", provider=self.name, grounding=Grounding.unverifiable,
                               warnings=[f"claude-code: exit {code}: {excerpt}"])
        try:
            result_text, model = self._parse_cli_json(stdout)
        except (ValueError, TypeError) as exc:
            return LLMResponse(text="", provider=self.name, grounding=Grounding.unverifiable,
                               warnings=[f"claude-code: unparseable CLI output: {exc}"])
        # inner {text, grounding, ...} — tolerate a model that answered in plain text
        text, grounding, data = result_text, Grounding.unverifiable, None
        try:
            inner = json.loads(result_text)
            if isinstance(inner, dict):
                data = inner
                text = str(inner.get("text", result_text))
                try:
                    grounding = Grounding(inner.get("grounding", "unverifiable"))
                except ValueError:
                    grounding = Grounding.unverifiable
            else:
                text = result_text
                grounding = Grounding.unverifiable
        except (ValueError, TypeError):
            grounding = Grounding.unverifiable
        return LLMResponse(text=text, provider=self.name, model=model, grounding=grounding, data=data)

    async def health(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._settings.claude_code_bin, "--version",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            return proc.returncode == 0
        except (FileNotFoundError, asyncio.TimeoutError, TimeoutError):
            return False
