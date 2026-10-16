# FastAPI Patterns (Catallax)

How the backend is structured. Read before adding/changing any backend code.

## Route Structure

- One router per domain: `backend/app/routers/<domain>.py`
  (e.g. `health.py`, later `prices.py`, `ledger.py`, `news.py`).
- Each module exposes `router = APIRouter(tags=["<domain>"])`.
- Routers are mounted in `app/main.py` with `prefix="/api/v1"`. **Not
  auto-discovered:** `main.py` hand-lists every mounted module in one explicit
  `for module in (health, prices, ..., reports, forecasts):` tuple — a new router
  is dead code (404 through nginx) until you add it to that tuple; a router
  added without that entry silently never mounts. Resource paths inside the router are plural nouns:
  `@router.get("/holdings")`.
- Versioning: breaking changes → mount the same router set under `/api/v2`;
  `/api/v1` stays until consumers are migrated.

## Dependency Injection

Everything a handler needs arrives via `Depends`. Never instantiate engines,
clients, or adapters inside handlers.

| Dependency | Provider | Module |
|---|---|---|
| Settings | `get_settings()` (lru_cached) | `app/config.py` |
| DB session | `get_db()` (async session per request) | `app/db.py` |
| Redis | `get_redis()` | `app/redis.py` |
| Price adapter | `get_price_adapter()` factory | `app/adapters/` |

Connection pools are created in the app `lifespan` (see `app/main.py`) and
disposed on shutdown. New long-lived resources follow the same pattern:
`init_*()` in lifespan startup, `dispose_*()` on shutdown, `get_*()` as the
`Depends` provider.

## Async Conventions

- All handlers and dependencies are `async def`. No blocking I/O in handlers.
- Blocking libraries (e.g. yfinance in Phase 2) run via
  `asyncio.to_thread(...)` inside their adapter — never in a route handler.
- Outbound HTTP (Fava, Wealthfolio, CoinGecko) uses `httpx.AsyncClient`.

## Error Handling Standard

Error envelope for all non-2xx responses:

```json
{ "error": { "code": "SYMBOL_NOT_FOUND", "message": "No data for symbol XYZ" } }
```

- Raise domain exceptions (subclass a project `CatallaxError`) in
  adapters/services; translate them to the envelope with
  `app.exception_handler(...)` registrations in `main.py`.
- Never leak raw upstream errors (yfinance tracebacks, Fava HTML) to clients.
- Health-style endpoints may return a status payload with 503 instead
  (see `routers/health.py`).

## Adapter Injection (Phase 2 pattern)

```python
# app/adapters/factory.py
def get_price_adapter(settings: Settings = Depends(get_settings)) -> PriceSourceAdapter:
    return ADAPTERS[settings.price_source_default]()

# app/routers/prices.py
@router.get("/prices/{symbol}")
async def get_price(symbol: str, adapter: PriceSourceAdapter = Depends(get_price_adapter)):
    return await adapter.get_quote(symbol)
```

Route handlers depend only on the `PriceSourceAdapter` interface
(`skills/data-adapters.md`) — never on a concrete implementation.

## Phase 2 routers (built)

Mounted under `/api/v1`: `prices` (`/prices/{symbol}`, `/prices/{symbol}/history`,
`/search`), `ledger` (`/ledger/accounts|networth|transactions|currencies`),
`news` (`/news`), `ws` (WebSocket `/ws/prices`).

- A catch-all `Exception` handler is registered alongside the `CatallaxError`
  and `RequestValidationError` handlers, so every REST non-2xx carries the
  envelope (generic message; detail is logged server-side, never leaked).
- `price_cache` is a write-through historical log; the **live** quote cache is
  Redis (per-asset-type TTL in the adapter base). Nothing reads `price_cache`
  back yet (Phase 4 consumer).
- The WS ticker caps symbols (50, ≤16 chars each), supports per-symbol source
  via `?symbols=BTC:coingecko,AAPL`, and degrades per-symbol on failure rather
  than tearing down the stream.

## Cockpit job pattern

The analyst cockpit (`app/routers/cockpit.py`, `app/services/cockpit.py`)
is the one router that runs an LLM agentic loop. It reuses the existing `ai_runs`
job/polling shape (`IntelService`) rather than inventing a new one:

- **Session storage.** `CockpitSession`/`CockpitMessage` (Postgres) hold the
  conversation; a session is a `session_key` (opaque uuid hex) plus its
  message history. No Redis state beyond the job queue below.
- **Ask → job id → poll.** `POST /sessions/{key}/ask` (`CockpitService.ask`)
  persists the user message and an `AiRun(kind="cockpit")` row in the same
  transaction, `LPUSH`es the job id onto the shared `ai_runs` queue, and
  returns `{job_id, message_id}` immediately (202) — it never runs the loop
  itself. The client then polls `GET /cockpit/jobs/{job_id}`, which is
  `IntelService.get` reshaped to the frontend's `{status, phase, frac,
  message?}` `JobStatus` contract (not `IntelService`'s own `AiJob` shape).
- **Where the turn actually runs.** `app/ai_worker.py` dequeues the job and
  dispatches on `kind`: `"cockpit"` goes to `CockpitService.execute` (mirrors
  `IntelService._execute`'s claim/heartbeat/persist shape), which calls
  `run_turn`. This only ever happens in the `ai-runner` container, which
  pins `LLM_PROVIDER=claude-code` in `docker-compose.yml` (never `.env`) —
  `run_turn` guards itself with a `RuntimeError` if `llm_provider == "noop"`,
  which is what the API container keeps, so an accidental in-API-container
  invocation fails loudly instead of silently running nothing.
- **The loop itself.** Each round is one application action: a stateless
  `claude -p ... --max-turns 2` call (`_build_argv`). `COCKPIT_MAX_TURNS`
  still bounds the Python dispatch loop; the CLI cap is not that loop.
  `--max-turns 2` is only room for CLI-internal structured-output formatting
  (host CLI 2.1.197 with `--max-turns 1` repeatedly returned
  `COCKPIT_PROVIDER_FAILURE subtype=error_max_turns api_status=0 turns=2`).
  Native tools stay off via explicit isolation: `--tools ''`,
  `--strict-mcp-config` + empty MCP, `--setting-sources ''`, and
  `BUILTIN_DENY_TOOLS` on `--disallowedTools` — raising the CLI cap does not
  enable a native tool loop. `--json-schema` still injects the CLI 2.1.197
  StructuredOutput serializer; the system prompt names that one native
  exception (call it exactly once to serialize a `tool_call` or `final`
  object). StructuredOutput does not execute finance tools — Python
  dispatches those from the schema object. Finance names stay out of
  `--allowedTools` and `--tools`. A blanket “do not use native tools”
  instruction conflicts with CLI enforcement (`You MUST call the
  StructuredOutput tool`) and burns the cap on retries. The full transcript
  is re-sent as the prompt every round; `_run_loop` (not the CLI) decides
  tool_call vs final. Tool calls dispatch only through the read-only
  `cockpit_tools.TOOLS` registry. A tool-call request naming anything
  outside the registry is refused (`REFUSAL_TEXT` persisted, loop stops) —
  no write path under any flag.
- **Limits.** `COCKPIT_MAX_TURNS` bounds the round count; the whole loop is
  wrapped in `asyncio.wait_for(..., COCKPIT_TIMEOUT_SECONDS)` (a timeout
  still persists an assistant message, never a silently lost turn);
  `COCKPIT_MAX_SESSION_MESSAGES` trims the context sent back each round
  (oldest-first, newest N kept); `AskRequest.text` caps at 4000 chars (422
  above that) so one question can't blow the context budget on its own.
- **Enable flag + share gate.** `COCKPIT_ENABLED` gates the whole router —
  off by default, every route 503s (`COCKPIT_DISABLED`) until a host turns
  it on deliberately. `_require_enabled` also requires `SHARE_PASSPHRASE` to
  be set (503 `COCKPIT_REQUIRES_SHARE_GATE` otherwise) — an LLM with
  read-only tool access to the whole portfolio must never be reachable on a
  host that never configured a share gate, the same requirement unredacted
  note export places on itself (`routers/notes.py::_export`).
