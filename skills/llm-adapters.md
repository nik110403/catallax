# LLM Adapters (Catallax) — Foundations F3

The LLM provider seam exists **before** any AI feature so Phase 12 can plug a
real model in without touching routers. Today the default provider is a **no-op**
that calls no model. Mirrors `PriceSourceAdapter`.

## Interface

```python
# backend/app/adapters/llm/base.py
class LLMMessage(BaseModel):
    role: Role            # system | user | assistant
    content: str

class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str | None
    data: dict[str, object] | None  # full parsed JSON object, when the model returned one
    grounding: Grounding  # grounded | speculative | unverifiable
    warnings: list[str]

class LLMProviderAdapter(ABC):
    name: str
    @abstractmethod
    async def complete(
        self, messages: list[LLMMessage], *, facts: dict | None = None
    ) -> LLMResponse: ...
    async def health(self) -> bool: ...
```

## The two hard rules (baked into the types)

1. **Deterministic compute is separate from the LLM.** Scores, grades, returns,
   risk metrics, allocations are computed in **pure Python** and are
   reproducible. The model only *narrates* numbers passed to it via `facts` — it
   never produces the numbers. A figure shown in the UI must trace to a Python
   computation, never to model output.
2. **Every claim is fact-grounded.** Narrative output is tagged:
   - `grounded` — traceable to a figure in `facts`.
   - `speculative` — model inference beyond the given facts.
   - `unverifiable` — cannot be checked.
   Consumers may surface the tag (badge) or filter to grounded-only.

## Factory & Registration

```python
# backend/app/adapters/llm/factory.py
LLM_PROVIDERS = { "noop": NoOpLLMAdapter, "claude-code": ClaudeCodeAdapter }
```

Selected by `LLM_PROVIDER` (default `noop`; the `ai-runner` container is the
only place that overrides it to `claude-code` — see `skills/ai-intel.md`).
Dependency: `get_llm_adapter`. Reserved env (unset by default): `LLM_BASE_URL`,
`LLM_MODEL`, `LLM_API_KEY` (not used by `claude-code`, which authenticates via
a mounted credentials directory, not a key — kept reserved for a future
key-based cloud provider). Ollama-local-first was the originally intended
first real provider; Phase 12 shipped `claude-code` instead (uses the operator's
existing subscription, no separate model-serving container) — Ollama remains
a valid future addition behind the same seam.

## `claude-code` — headless Claude Code CLI (Phase 12, first real provider)

`backend/app/adapters/llm/claude_code.py::ClaudeCodeAdapter` runs `claude -p
<prompt> --output-format json --max-turns <N>` as an async subprocess (scratch
cwd `/tmp`, hard timeout). Only ever invoked inside the off-API `ai-runner`
worker (`--profile ai`) — the sole container with the `claude` binary and a
read-only mount of the host's `~/.claude` credentials dir
(`CLAUDE_CODE_CREDENTIALS_PATH`); the main API container keeps `LLM_PROVIDER=
noop` so it never shells out to an LLM.

**Fail-safe contract — every failure path returns, never raises:** CLI
timeout, missing binary, non-zero exit, or unparseable/non-JSON-object output
all resolve to `LLMResponse(grounding=unverifiable, warnings=[...])`. A model
reply that isn't a JSON object (plain prose) is tolerated too — the raw string
becomes `text`, tagged `unverifiable`, never an exception up the call stack.

**`LLMResponse.data` (the seam extension):** when the model replies with a
JSON object, the full parsed dict is returned as `data` (in addition to
`text`, kept for narration-only callers). Structured callers — e.g. news
enrichment (`skills/ai-intel.md`) — read typed fields off `data` rather than
re-parsing `text`. Both `text` and `data` are UNTRUSTED model output; any
consumer rendering them must escape at the render edge, never inject as raw
markup (`dangerouslySetInnerHTML`, MapLibre `setHTML`, etc.).

## How to add a real provider

1. Implement `LLMProviderAdapter` in `backend/app/adapters/llm/<name>.py`.
2. Register it in `LLM_PROVIDERS`.
3. Wire its env (base URL / model / key, or an auth-mount pattern like
   `claude-code`'s) in `.env.example`.
4. Enforce rule 1 in the calling service (compute in Python, pass `facts`) and
   rule 2 in the provider (tag every claim). Update this file.

## Status

- `NoOpLLMAdapter` echoes the last user message, tags `unverifiable`, and warns
  it called no model — nothing downstream can mistake an echo for analysis.
  Default everywhere except `ai-runner`.
- `ClaudeCodeAdapter` is the first real provider — see above +
  `skills/ai-intel.md` for the full enrichment pipeline it powers.
- Unit-tested in `backend/tests/test_llm_seam.py`; the default stack still
  makes **zero runtime model calls** (only `ai-runner`, opt-in, calls out).
