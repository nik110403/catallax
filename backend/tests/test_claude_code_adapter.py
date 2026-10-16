import asyncio
import json
import pytest
from app.adapters.llm.base import Grounding, LLMMessage, Role
from app.adapters.llm.claude_code import ClaudeCodeAdapter
from app.config import get_settings


def _adapter():
    return ClaudeCodeAdapter(get_settings())


def test_prompt_embeds_facts_and_grounding_rule():
    a = _adapter()
    prompt = a._build_prompt(
        [LLMMessage(role=Role.user, content="Summarise this headline.")],
        facts={"title": "ACME beats earnings", "source": "Reuters"},
    )
    assert "ACME beats earnings" in prompt
    assert "Reuters" in prompt
    # grounding contract must be instructed
    assert "grounded" in prompt and "speculative" in prompt
    assert "JSON" in prompt or "json" in prompt


def test_parse_cli_json_extracts_result_and_model():
    # `claude -p --output-format json` returns an envelope with a `result` string.
    payload = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": json.dumps({"text": "ACME beat estimates.", "grounding": "grounded"}),
        "model": "claude-opus-4-8",
    })
    text, model = ClaudeCodeAdapter._parse_cli_json(payload)
    assert "ACME beat estimates" in text
    assert model == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_complete_success(monkeypatch):
    a = _adapter()

    async def fake_run(prompt: str) -> tuple[int, str, str]:
        inner = json.dumps({"text": "Positive tone.", "grounding": "speculative"})
        env = json.dumps({"result": inner, "model": "claude-opus-4-8", "is_error": False})
        return 0, env, ""

    monkeypatch.setattr(a, "_run_cli", fake_run)
    resp = await a.complete(
        [LLMMessage(role=Role.user, content="tone?")],
        facts={"title": "x"},
    )
    assert resp.provider == "claude-code"
    assert resp.model == "claude-opus-4-8"
    assert resp.grounding == Grounding.speculative
    assert "Positive tone" in resp.text


@pytest.mark.asyncio
async def test_complete_success_carries_structured_data(monkeypatch):
    a = _adapter()

    async def fake_run(prompt: str) -> tuple[int, str, str]:
        inner = json.dumps({
            "text": "Fed holds rates.",
            "grounding": "speculative",
            "sentiment": "positive",
            "sentiment_score": 0.3,
            "topics": ["fed", "inflation"],
            "country": "United States",
            "lat": 38.89,
            "lon": -77.03,
        })
        env = json.dumps({"result": inner, "model": "claude-opus-4-8", "is_error": False})
        return 0, env, ""

    monkeypatch.setattr(a, "_run_cli", fake_run)
    resp = await a.complete(
        [LLMMessage(role=Role.user, content="classify")],
        facts={"title": "x"},
    )
    assert resp.data is not None
    assert resp.data["sentiment"] == "positive"
    assert resp.data["topics"] == ["fed", "inflation"]
    assert resp.data["country"] == "United States"


@pytest.mark.asyncio
async def test_complete_timeout_returns_warning(monkeypatch):
    a = _adapter()

    async def fake_timeout(prompt: str):
        raise TimeoutError("claude timed out")

    monkeypatch.setattr(a, "_run_cli", fake_timeout)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.grounding == Grounding.unverifiable
    assert any("tim" in w.lower() for w in resp.warnings)
    assert resp.text == ""


@pytest.mark.asyncio
async def test_complete_nonzero_exit_returns_warning(monkeypatch):
    a = _adapter()

    async def fake_fail(prompt: str):
        return 1, "", "not logged in"

    monkeypatch.setattr(a, "_run_cli", fake_fail)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.grounding == Grounding.unverifiable
    assert resp.warnings


@pytest.mark.asyncio
async def test_complete_nonzero_exit_with_empty_stderr_uses_stdout_json_result(monkeypatch):
    # Failure mode: the CLI transiently exits 1 with EMPTY stderr but a
    # JSON error envelope on stdout — the warning must surface the parsed
    # `result` field, not a useless "exit 1: ".
    a = _adapter()

    async def fake_fail(prompt: str):
        stdout = json.dumps({
            "type": "result", "subtype": "error", "is_error": True,
            "result": "401 OAuth access token has been revoked",
        })
        return 1, stdout, ""

    monkeypatch.setattr(a, "_run_cli", fake_fail)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.grounding == Grounding.unverifiable
    assert resp.warnings == ["claude-code: exit 1: 401 OAuth access token has been revoked"]


@pytest.mark.asyncio
async def test_complete_nonzero_exit_with_non_json_stdout_uses_raw_tail(monkeypatch):
    a = _adapter()

    async def fake_fail(prompt: str):
        return 1, "not json output at all", ""

    monkeypatch.setattr(a, "_run_cli", fake_fail)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.warnings == ["claude-code: exit 1: not json output at all"]


@pytest.mark.asyncio
async def test_complete_nonzero_exit_with_empty_stdout_falls_back_to_stderr(monkeypatch):
    a = _adapter()

    async def fake_fail(prompt: str):
        return 1, "", "not logged in"

    monkeypatch.setattr(a, "_run_cli", fake_fail)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.warnings == ["claude-code: exit 1: not logged in"]


@pytest.mark.asyncio
async def test_complete_nonzero_exit_redacts_secrets_and_strips_newlines(monkeypatch):
    # Security review follow-up: a credential leaked into a CLI error message
    # must never land unredacted in the persisted/rendered warning, and a
    # newline must never survive into the single-line warning (log-forging,
    # CWE-117).
    a = _adapter()

    async def fake_fail(prompt: str):
        return 1, "auth failed: token sk-ant-oat01-abc\nplease re-login", ""

    monkeypatch.setattr(a, "_run_cli", fake_fail)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.warnings == ["claude-code: exit 1: auth failed: token [redacted] please re-login"]
    assert "sk-ant-oat01-abc" not in resp.warnings[0]
    assert "\n" not in resp.warnings[0]


def test_sanitize_excerpt_redacts_known_secret_shapes():
    assert ClaudeCodeAdapter._sanitize_excerpt("key=sk-ant-oat01-abc123") == "key=[redacted]"
    assert ClaudeCodeAdapter._sanitize_excerpt("Authorization: Bearer abc.def.ghi") == "Authorization: [redacted]"
    assert ClaudeCodeAdapter._sanitize_excerpt("token oat01-xyz789") == "token [redacted]"
    assert ClaudeCodeAdapter._sanitize_excerpt("line1\nline2\x00\x7f") == "line1 line2 "


@pytest.mark.asyncio
async def test_complete_non_dict_json_returns_unverifiable(monkeypatch):
    a = _adapter()

    async def fake_run(prompt: str) -> tuple[int, str, str]:
        # result is valid JSON ("42") but not a dict once parsed by the inner
        # json.loads(result_text) — it decodes to the int 42.
        env = json.dumps({"result": "42", "model": "m", "is_error": False})
        return 0, env, ""

    monkeypatch.setattr(a, "_run_cli", fake_run)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.grounding == Grounding.unverifiable


# ── review-225d5f4 re-review F5 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_cli_kills_and_reaps_the_subprocess_on_cancel(monkeypatch):
    """A caller-side cancel (the report-worker's hard bound around
    enrichment, or narration's own wait_for) used to fall through unkilled
    — only a TimeoutError from `_run_cli`'s own wait_for killed the child.
    The subprocess must be killed and reaped, and the cancellation itself
    still re-raised (never swallowed)."""
    a = _adapter()
    killed: list[bool] = []
    waited: list[bool] = []

    class _FakeProc:
        returncode = None

        async def communicate(self):
            await asyncio.Event().wait()  # hangs until the caller is cancelled

        def kill(self):
            killed.append(True)

        async def wait(self):
            waited.append(True)
            self.returncode = -9

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    task = asyncio.ensure_future(a._run_cli("prompt"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed == [True]
    assert waited == [True]


@pytest.mark.asyncio
async def test_bad_grounding_keeps_data(monkeypatch):
    a = _adapter()

    async def fake_run(prompt: str) -> tuple[int, str, str]:
        inner = json.dumps({"text": "x", "grounding": "weird", "sentiment": "positive"})
        env = json.dumps({"result": inner, "model": "m", "is_error": False})
        return 0, env, ""

    monkeypatch.setattr(a, "_run_cli", fake_run)
    resp = await a.complete([LLMMessage(role=Role.user, content="x")], facts={})
    assert resp.data == {"text": "x", "grounding": "weird", "sentiment": "positive"}
    assert resp.grounding == Grounding.unverifiable
