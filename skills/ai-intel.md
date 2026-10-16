# AI Intelligence Layer — `--profile ai`

News enrichment on top of the F3 `LLMProviderAdapter` seam, using **Claude Code
(headless CLI)** as the first real provider — chosen over a local Ollama model
so the terminal narrates real financial content on the operator's existing subscription
rather than standing up a second heavy GPU/CPU-bound service. Off by default;
enable with `docker compose --profile ai up -d ai-runner`.

Read this before touching `backend/app/adapters/llm/claude_code.py`,
`backend/app/services/intel.py`, `backend/app/services/intel_service.py`,
`backend/app/ai_worker.py`, `backend/app/routers/intel.py`, or the AI tab
frontend widgets / the GeoMap AI-events layer.

## The provider seam + grounding contract (recap — full spec in `skills/llm-adapters.md`)

Two rules baked into the `LLMResponse` type, unchanged from F3:

1. **Deterministic compute is separate from the LLM.** Every number the model
   might mention (title, source, published date) is computed/fetched in Python
   and handed in via `facts=`; the model only narrates or classifies.
2. **Every claim is fact-grounded** — `grounded` / `speculative` /
   `unverifiable`. Enrichment tags summary + sentiment `speculative` by
   construction (they're model inference); only title/source/date are facts.

## The `claude-code` provider

`ClaudeCodeAdapter` (`name = "claude-code"`, registered in the `LLM_PROVIDERS`
factory) runs the headless CLI as an async subprocess: `claude -p <prompt>
--output-format json --max-turns <N>`, cwd `/tmp` (scratch — no repo/project
access), hard timeout (`CLAUDE_CODE_TIMEOUT_SECONDS`). The prompt embeds a
system instruction demanding a single JSON object
`{"text", "grounding", ...}` and a `FACTS:` block (the Python-computed facts,
JSON-dumped) before the task.

**Fail-safe by construction — never raises to the caller.** Every failure mode
returns an `LLMResponse(text="", grounding=unverifiable, warnings=[...])`
instead of propagating an exception:
- CLI timeout → killed + `TimeoutError` caught → unverifiable + warning.
- Missing binary (`FileNotFoundError`, e.g. ai-runner image not built) →
  unverifiable + warning naming the cause.
- Non-zero exit → unverifiable + warning with the exit code + truncated stderr.
- CLI stdout not parseable as the wrapper envelope JSON → unverifiable + warning.
- The **inner** model reply not a JSON object (plain prose, a list, `null`) →
  tolerated: the raw string becomes `text`, grounding forced `unverifiable`.

`health()` runs `claude --version` with its own timeout — used by
`GET /intel/status` for a cheap liveness check without spending a real turn.

### The `LLMResponse.data` seam extension

`LLMResponse` gained a `data: dict[str, object] | None` field (F3 base type,
`skills/llm-adapters.md`) carrying the **full parsed JSON object** when the
model replied with one. Narration-only callers keep reading `text`; structured
callers — the enrichment path — read typed fields off `data` instead of
re-parsing `text` themselves. Both are UNTRUSTED model output like `text` and
must never be trusted as raw HTML/markup.

This was a **verify-time seam fix**: the first end-to-end enrich run showed
sentiment/topics/country all null even though the model was replying correctly
— `IntelEngine.enrich_item` was re-`json.loads`-ing `resp.text`, but the
adapter's inner-JSON handling had already consumed it into `text` as a plain
string in some paths. Passing the parsed dict through explicitly on the seam
(`data`) closed the gap; re-verified afterward (see Verification section).

## Runtime: `ai-runner` (the only container with `claude` + creds)

`backend/Dockerfile.ai` = the same backend source as the main image, plus
Node.js + `@anthropic-ai/claude-code` (npm global install) so the CLI binary
is on `PATH`; Python deps unchanged (already declared in `pyproject.toml`, no
new heavy deps — mirrors the Phase-9/11 pattern of a purpose-built image
without polluting the daily backend).

Compose service **`ai-runner`** (`docker-compose.yml`):
- `profiles: ["ai"]` — opt-in only, same as `backtest`/`lab`.
- `build: { context: ./backend, dockerfile: Dockerfile.ai }`.
- Runs `python -m app.ai_worker` — **off-API**, no HTTP port, mirrors
  `worker.py` (backtester) / `lab_worker.py` (Quant Lab) exactly: claim from
  Redis, run, persist, publish SSE, heartbeat, crash-recovery.
- `environment: LLM_PROVIDER=claude-code` — **only this container** overrides
  the default. The main `fastapi-backend` (and every other service) keeps
  `LLM_PROVIDER=noop`, so a stack with `ai-runner` off makes **zero** LLM calls
  anywhere, satisfying the Phase-12 acceptance criterion.
- `depends_on: postgres, redis (service_healthy)`.

### Auth: mount the host Claude Code credentials, never copy them into the image

`volumes: - ${CLAUDE_CODE_CREDENTIALS_PATH:-/dev/null}:/root/.claude:ro` — the
host's existing `~/.claude` credentials directory (the logged-in subscription
session) is bind-mounted **read-only** into the container. This uses the operator's
existing Claude subscription rather than provisioning a separate API key:
- No credential ever enters the image layer or a committed file — it is
  mounted at container start from an env-var path (`.env`, gitignored).
- The default `/dev/null` fallback means an unset path mounts a harmless file,
  not the real host directory, if `--profile ai` is started without configuring it.
- Credentials never reach `fastapi-backend`, `react-frontend`, or any other
  service — only `ai-runner` gets the mount.

## Data model (migration `0009`, `down_revision = "0008"`)

Mirrors the `lab_runs`/`backtests` job-table pattern: one row **is** both the
job and its (eventual) result.

- **`ai_runs`** — job store: `job_id` (UUID, unique), `kind` (`"enrich"`),
  `config` (JSONB), `status` (`queued`/`running`/`done`/`failed`), `progress`
  (float), `phase`, `result` (JSONB — `{enriched, failed, clusters}`), `error`
  (JSONB envelope), `warnings`, `worker_id`, `heartbeat_at`, timestamps.
  Indexed on `status` + `created_at`.
- **`news_intel`** — one row per enriched article, **idempotent upsert on
  `link`** (`UniqueConstraint`): `title`, `source`, `published`, `sentiment`
  (positive/neutral/negative), `sentiment_score` (−1..1), `topics` (JSONB
  array, ≤4 tags), `country`, `lat`/`lon` (nullable — most company/stock news
  has no coordinates), `summary` (model-written, ≤400 chars), `cluster_id`
  (≤36-char string — see hashing below), `grounding`, `enriched_at`. Indexed on
  `published` + `cluster_id`.

## The enrichment engine (`services/intel.py::IntelEngine`)

Pure orchestration, no Redis/DB — `IntelService` (below) owns persistence.

`enrich_item(adapter, item)` builds a `facts` dict of only the real fields
(title/source/published/summary from the RSS item) and a task prompt asking
for a JSON object: `text` (1-sentence summary), `grounding`, `sentiment`,
`sentiment_score`, `topics` (≤4 tags), `country`, `lat`/`lon`. **Never raises**
— any adapter failure (warnings present, or empty text) or unparseable/
non-dict response degrades to `Enrichment(grounding="unverifiable")` with the
fields left `None`, so a failed article is still recorded, never dropped
silently. Numeric fields are defensively clamped (`sentiment_score` to
[-1,1], `lat`/`lon` to valid ranges) and strings are length-capped before
they ever reach a DB column.

### Clustering (`cluster(rows)`)

Deterministic union-find over shared `topics`: two articles sharing ≥1 topic
tag join the same cluster; topic-less articles are singletons. `cluster_id =
sha1(link of the cluster's root article)[:16]` — **no RNG, stable across
re-runs**, and deliberately truncated to fit the `news_intel.cluster_id
String(36)` column (a full sha1 hex digest is 40 chars and would not fit —
this was a review fix, see Session changelog).

## The off-API job lifecycle (`services/intel_service.py::IntelService`)

Mirrors `BacktestService`/`LabService` byte-for-byte in shape: Redis reliable
queue (`catallax:ai:queue` / `catallax:ai:processing`), `recover()` on worker
start (requeues orphaned `running` rows past `heartbeat_at` + `AI_STALE_SECONDS`,
or rows popped off `processing` without a terminal status), `_heartbeat()` loop
(`AI_HEARTBEAT_SECONDS`), SSE `_publish()` over `catallax:ai:progress:{job_id}`,
and `AI_MAX_QUEUED` as an enqueue flood-guard (`InvalidRequest` once in-flight
jobs hit the cap).

`execute()`: claim (atomic `UPDATE … WHERE status='queued'`, no-op if already
claimed) → fetch news (`get_news`, capped at `AI_ENRICH_MAX_ARTICLES`) →
`enrich_item` per article (progress published every 5 articles) → `cluster()`
→ upsert every row (`pg_insert(...).on_conflict_do_update(index_elements=[link])`)
→ persist `result`/`status=done`. Any unhandled exception is caught, persisted
as `status=failed` + an error envelope, and published over SSE — the worker
loop itself never crashes.

**Budget + cancel (2026-09-28).** `execute(job_id, worker_id, deadline=…)` takes an
optional `time.monotonic()` deadline: once passed, no further article goes to the LLM,
the articles enriched so far are clustered + upserted, and `result` gains
`truncated_at` / `available` (never padded). Overshoot ≤ one `CLAUDE_CODE_TIMEOUT_SECONDS`
call. The weekly report-worker runs enrich in-process with `REPORT_PACK_INTEL_BUDGET_SECONDS`
(default 600, clamped to job cap − one claude call) as a **hard** `asyncio.timeout` around
the whole task (news fetch + LLM calls + persist), with the per-article soft deadline one
claude call + 30 s earlier; the task runs **independently** of the factor study (panel
fetch AND computation) — in one earlier run, unbudgeted sequential enrichment at ~46 s/article
ate the whole 1200 s `REPORT_PACK_JOB_TIMEOUT_SECONDS` cap and the pack shipped stale factors.

**Terminal-state guarantee.** Queue-less callers use
`run_inline(job_id, worker_id, deadline=…)`: it inserts and executes the row in-process
(no Redis queue entry at all) and, on ANY non-success exit — error, cancel, including a
cancel between the insert and `_execute`'s claim — writes `failed` (`phase=cancelled` /
`interrupted`, shielded). Every failure write goes through `_fail_if_unfinished`, a
conditional `UPDATE … WHERE status IN ('queued','running')`, so a cancel or error landing
after the `done` commit (e.g. during the completion publish) never rewrites the success.

`app/ai_worker.py` mirrors `worker.py`/`lab_worker.py`: `recover()` once, then
loop `BRPOPLPUSH`-equivalent claim + `execute()`.

### Optional lifespan auto-enrich

`AI_ENRICH_INTERVAL_SECONDS` (default `0` = disabled, manual-only via "Enrich
now"). When >0, a lifespan background task enqueues an enrich job on that
cadence — the same pattern as the Session-26 breadth auto-fetch task.

## Routes (`routers/intel.py`, all `/api/v1`)

| Method · path | Purpose |
|---|---|
| `POST /intel/enrich` | enqueue an enrich job (off-API), 429-style `InvalidRequest` if `AI_MAX_QUEUED` in flight |
| `GET /intel/runs` | recent jobs (list) |
| `GET /intel/runs/{id}` | one job's status (poll target) |
| `GET /intel/runs/{id}/stream` | SSE live progress (Redis pub/sub, terminates on `done`/`failed`) |
| `GET /intel/news` | enriched articles, newest-published-first |
| `GET /intel/events` | subset of `/intel/news` with non-null `lat`/`lon` — feeds the GeoMap layer |
| `GET /intel/status` | `{provider, healthy}` — builds the configured adapter and calls `health()` |

`{id}` is a server-minted UUID, route-alphabet-bounded (`^[A-Za-z0-9\-]{8,36}$`).
The API container only INSERTs a queued row and SELECTs — it never runs
`claude` itself (it stays on `LLM_PROVIDER=noop`), so `/intel/enrich` can
never block the event loop even if the CLI hangs.

## Frontend — AI tab (namespace `ai`, palette `AI`, workspaces **v11**)

Three widgets, category `"AI"`:
- **`AINews.tsx`** (`ai-news`, lazy-registered) — enriched feed: sentiment
  chip, country tag, topic labels, grounded/ungrounded dot, model summary.
  RSS `link` is untrusted — `safeHref()` allows only `http(s)://` (blocks
  `javascript:`/`data:` injection via a malicious feed entry); rendered through
  normal React text nodes, never `dangerouslySetInnerHTML`.
- **`IntelRunner.tsx`** (`ai-intel-runner`) — "Enrich now" button → `POST
  /intel/enrich` → the shared `<JobStatus>` SSE component streams progress to
  completion.
- **`AIStatus.tsx`** (`ai-status`) — provider health card (`GET /intel/status`).

### GeoMap "AI Events" layer

`GeoMap.tsx` gained a fourth toggleable layer (`ai_events_layer`, alongside
financial-hubs/sanctions/quakes): `GET /intel/events` → a GeoJSON point
`FeatureCollection`, markers colored by sentiment, click popup shows
title/summary/source. Because this popup renders **untrusted RSS-derived**
title/summary via MapLibre's raw `setHTML`, it goes through an explicit
`escapeHtml()` helper first — unlike the map's other three popup branches
(quakes/hubs/sanctions), which use curated/USGS data and were left on raw
`setHTML` (lower risk, logged as a follow-up hardening item, not a blocker).

## Config (`.env.example`)

`CLAUDE_CODE_BIN` (default `claude`), `CLAUDE_CODE_CREDENTIALS_PATH` (host
`~/.claude` path, mounted `:ro`), `CLAUDE_CODE_TIMEOUT_SECONDS`,
`CLAUDE_CODE_MAX_TURNS`, `AI_MAX_CONCURRENCY`, `AI_HEARTBEAT_SECONDS`,
`AI_STALE_SECONDS`, `AI_POLL_TIMEOUT_SECONDS`, `AI_MAX_QUEUED`,
`AI_ENRICH_INTERVAL_SECONDS` (0 = manual-only), `AI_ENRICH_MAX_ARTICLES`.
`LLM_PROVIDER` stays `noop` everywhere except `ai-runner`.

## Verification

Hard-gate probe: ran real `claude -p` **inside the container** with the host
creds mounted → returned grounded JSON (proved the mount + CLI + auth chain
before building the rest). Full enrich run: **40 articles** enriched
end-to-end (SSE 0→1.0 through `fetching news`/`enriching i/N`/`persisting`/
`done`; `ai_runs` row `status=done`; 40 `news_intel` rows persisted). After the
`LLMResponse.data` seam fix, re-verified: sentiment/score/topics/country all
populate (previously null) and `grounding="speculative"` shows correctly for
model-inferred fields. AI tab: design-reviewer PASS/ship, 0 console errors,
all 3 widgets render with real data. Backend unit suite 45/45.

## Honest limitations

- Enrichment is **per-article model inference**, not deterministic compute —
  sentiment/summary are always tagged `speculative` (never `grounded`); only
  title/source/published-date are facts. A failed article (timeout, bad JSON,
  missing binary) is persisted with `grounding="unverifiable"` and `null`
  structured fields — **never silently dropped** from `news_intel`.
- Geo-tags are **model-inferred**, not geocoded from a gazetteer — company/
  stock-specific news has no natural coordinates and correctly comes back
  `lat=lon=null` (excluded from `/intel/events`, so the GeoMap layer only ever
  shows articles the model could plausibly place).
- Full-run cost scales linearly with `AI_ENRICH_MAX_ARTICLES` × one `claude`
  CLI invocation per article — tune the cap before raising `AI_MAX_QUEUED` or
  enabling the auto-enrich interval.
- `news_intel` rows enriched **before** the `LLMResponse.data` seam fix have
  null structured fields (sentiment/topics/country/summary) even though the
  row exists — a fresh `POST /intel/enrich` overwrites them on the next
  `link`-keyed upsert; there is no automatic backfill.
- Ollama-local-first (the originally intended first real provider per F3 /
  `.env.example`) is **deferred** — Claude Code was chosen instead because it
  uses the operator's existing subscription, needs no separate GPU/model-serving
  container, and the seam is provider-agnostic, so swapping in Ollama later is
  a new adapter + one `LLM_PROVIDERS` registration, no router changes.
