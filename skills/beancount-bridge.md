# Beancount Bridge (Catallax)

How ledger data flows from Beancount/Fava into the API. Implementation lands
in Phase 2 (consumed by Phase 4 views); this is the contract.

## Architecture

```
React widgets ──> FastAPI /api/v1/ledger/* ──> Fava JSON API (http://fava:5000/fava/...)
                                      └─(fallback)─> bean-query against /data ledger
```

- The frontend **never** calls Fava directly (hard constraint). Nginx exposes
  `/fava/` only for the human-facing Fava UI.
- Primary query path: Fava's JSON API (`/fava/<ledger-slug>/api/...`) via
  `httpx.AsyncClient`, base URL from `FAVA_BASE_URL`.
- Fallback path (noted, not default): run `bean-query` directly against the
  ledger file if a query isn't covered by Fava's API. Requires beancount in
  the backend image — add only when actually needed.

## Normalisation

Raw Fava/bean-query output is normalised into internal models before leaving
the bridge module (`backend/app/services/ledger.py`):

```python
class AccountNode(BaseModel):      # account tree
    name: str                      # full name, e.g. "Assets:EU:Bank:Checking"
    balances: dict[str, Decimal]   # currency -> amount
    children: list["AccountNode"]

class LedgerTransaction(BaseModel):
    date: date
    payee: str | None
    narration: str
    postings: list[Posting]        # account, amount, currency, cost

class NetWorthPoint(BaseModel):
    date: date
    value: Decimal
    currency: str                  # reporting currency
```

Rules:
- Amounts are `Decimal`, never float.
- Multi-currency balances stay per-currency; conversion to a reporting
  currency happens in one place (the bridge), using the ledger's price
  directives or the price adapter.
- Account names keep full Beancount paths; UI shortens for display.

## Endpoints (Phase 2/4)

| Endpoint | Returns |
|---|---|
| `GET /api/v1/ledger/accounts` | `AccountNode` tree with balances |
| `GET /api/v1/ledger/networth?currency=EUR&interval=month` | `NetWorthPoint[]` |
| `GET /api/v1/ledger/transactions?account=...&from=...&to=...` | `LedgerTransaction[]` |
| `GET /api/v1/ledger/currencies` | per-currency totals breakdown |

## Operational Notes

- The ledger file is whatever `BEANCOUNT_FILE` points at; the bridge never
  hardcodes a path or slug — Fava's ledger slug is discovered at startup.
- Bridge responses are cached in Redis keyed on (endpoint, params, ledger
  mtime) — invalidated when the ledger file changes.
- Fava unreachable → bridge raises `LedgerUnavailable` → standard error
  envelope with 503; widgets show their own error state.

## Implementation notes (Phase 2 — built)

- **Path chosen:** Fava's JSON query API (`GET /<slug>/api/query?query_string=<BQL>`),
  not the bean-query fallback. Responses are `{"data":{"rows":[...]}}`; JSON is
  parsed with `parse_float=Decimal` so money stays exact.
- **Slug discovery:** `GET <FAVA_BASE_URL>/` 302-redirects to `/fava/<slug>/…`;
  the slug is parsed from `Location` and cached in Redis (`fava:slug`, 10 min).
- **Caching:** normalised results cached in Redis keyed `ledger:<key>:<mtime>`,
  where mtime is `BEANCOUNT_FILE`'s (the `./data` dir is mounted read-only into
  the backend purely to stat it). TTL `LEDGER_CACHE_TTL_SECONDS`.
- **Input safety:** `currency` and `account` are interpolated into BQL, so both
  are allowlist-validated (`^[A-Z][A-Z0-9._-]{1,23}$` / `^[A-Za-z0-9:_-]{1,128}$`)
  and rejected with `INVALID_REQUEST` (400) otherwise. The BQL string and raw
  upstream errors are logged server-side, never returned to the client.
- **`/transactions` is register-style:** with an `account` filter the result
  contains only the postings whose account matches (prefix-anchored `^`), not the
  full balanced leg set of each entry.
