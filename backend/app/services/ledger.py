"""Beancount bridge — ledger data via Fava's JSON query API.

The frontend never talks to Fava directly (hard constraint); this module is
the only caller. It discovers Fava's ledger slug at startup (via the redirect
from the ledger root), runs BQL through `/<slug>/api/query`, and normalises the
raw rows into the internal models below. Results are cached in Redis keyed on
the ledger file mtime so a ledger edit invalidates the cache.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import Settings
from app.errors import InvalidRequest, LedgerUnavailable, UpstreamError
from app.services.demo import (
    ledger_accounts_rows,
    ledger_cash,
    ledger_currencies,
    ledger_query_rows,
    ledger_transactions,
    ledger_value_asof,
)

logger = logging.getLogger("catallax.ledger")

_SLUG_KEY = "fava:slug"
_SLUG_TTL = 600

# Allowlists for values interpolated into BQL. Beancount commodities are
# uppercase alphanumerics; account names are ':'-separated [A-Za-z0-9-] segments.
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9._-]{1,23}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


def _validate_currency(currency: str) -> str:
    if not _CURRENCY_RE.fullmatch(currency):
        raise InvalidRequest(f"Invalid currency: {currency!r}")
    return currency


def _validate_account(account: str) -> str:
    if not _ACCOUNT_RE.fullmatch(account):
        raise InvalidRequest(f"Invalid account filter: {account!r}")
    return account


# ── normalised models ────────────────────────────────────────
class Posting(BaseModel):
    account: str
    amount: Decimal
    currency: str


class AccountNode(BaseModel):
    name: str
    balances: dict[str, Decimal] = {}
    children: list["AccountNode"] = []


class LedgerTransaction(BaseModel):
    date: date
    payee: str | None = None
    narration: str
    postings: list[Posting] = []


class NetWorthPoint(BaseModel):
    date: date
    value: Decimal
    currency: str


class CurrencyTotal(BaseModel):
    currency: str
    units: Decimal
    value: Decimal  # converted to reporting currency
    reporting_currency: str


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class LedgerBridge:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── infrastructure ───────────────────────────────────────
    @property
    def _base(self) -> str:
        return self._settings.fava_base_url.rstrip("/")

    def _mtime(self) -> int:
        # Cache-bust key: the NEWEST mtime across every .bean file in the ledger
        # tree, not just the root file. The root only `include`s the data files
        # (transactions/*.bean, opening_balances.bean, …), so editing those leaves
        # the root's mtime untouched — keying on it alone served stale balances
        # until the TTL expired. Falls back to the root file's mtime if the glob
        # finds nothing (e.g. a single-file ledger).
        root = self._settings.beancount_file
        try:
            ledger_dir = os.path.dirname(root) or "."
            mtimes = [
                os.path.getmtime(p)
                for p in glob.glob(os.path.join(ledger_dir, "**", "*.bean"), recursive=True)
            ]
            if not mtimes:
                mtimes = [os.path.getmtime(root)]
            return int(max(mtimes))
        except OSError:
            return 0

    async def _slug(self, client: httpx.AsyncClient) -> str:
        cached = await self._redis.get(_SLUG_KEY)
        if cached:
            return cached
        try:
            resp = await client.get(f"{self._base}/", follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LedgerUnavailable(f"Fava unreachable: {exc}") from exc
        location = resp.headers.get("location", "")
        # e.g. /fava/<slug>/income_statement/
        parts = [p for p in location.split("/") if p]
        if "fava" in parts:
            idx = parts.index("fava")
            if idx + 1 < len(parts):
                slug = parts[idx + 1]
                await self._redis.set(_SLUG_KEY, slug, ex=_SLUG_TTL)
                return slug
        raise LedgerUnavailable(
            f"Could not discover Fava ledger slug (location={location!r})"
        )

    async def _query(self, client: httpx.AsyncClient, slug: str, bql: str) -> list:
        url = f"{self._base}/{slug}/api/query"
        try:
            resp = await client.get(url, params={"query_string": bql})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Log the detail (incl. the BQL) server-side; client sees generic.
            logger.warning("Fava query failed: %s (bql=%s)", exc, bql)
            raise UpstreamError("Ledger query failed") from exc
        # Preserve numeric precision: parse JSON floats straight to Decimal.
        body = json.loads(resp.text, parse_float=Decimal)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or "rows" not in data:
            logger.warning("Unexpected Fava response for bql=%s", bql)
            raise UpstreamError("Unexpected response from ledger backend")
        return data["rows"]

    async def query_rows(self, bql: str) -> list:
        """Public BQL passthrough for sibling services (e.g. the portfolio
        engine). Callers MUST validate any value interpolated into `bql`
        (see app/services/portfolio.py)."""
        async with httpx.AsyncClient(timeout=20) as client:
            slug = await self._slug(client)
            return await self._query(client, slug, bql)

    async def _cached(self, key: str, model_cls, build):
        full = f"ledger:{key}:{self._mtime()}"
        raw = await self._redis.get(full)
        if raw is not None:
            try:
                return [model_cls.model_validate(d) for d in json.loads(raw)]
            except Exception as exc:
                logger.debug("Ledger cache deserialise failed for %s: %s", full, exc)
        models = await build()
        try:
            await self._redis.set(
                full,
                json.dumps([m.model_dump(mode="json") for m in models]),
                ex=self._settings.ledger_cache_ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Ledger cache write failed for %s: %s", full, exc)
        return models

    # ── public API ───────────────────────────────────────────
    async def accounts(self) -> list[AccountNode]:
        async def build() -> list[AccountNode]:
            async with httpx.AsyncClient(timeout=15) as client:
                slug = await self._slug(client)
                rows = await self._query(
                    client,
                    slug,
                    "SELECT account, sum(position) AS bal "
                    "GROUP BY account ORDER BY account",
                )
            return self._build_tree(rows)

        return await self._cached("accounts", AccountNode, build)

    async def networth(
        self, currency: str, months: int
    ) -> list[NetWorthPoint]:
        currency = _validate_currency(currency or self._settings.reporting_currency)

        async def build() -> list[NetWorthPoint]:
            ends = _month_ends(months)
            async with httpx.AsyncClient(timeout=20) as client:
                slug = await self._slug(client)

                async def point(d: date) -> NetWorthPoint:
                    bql = (
                        f"SELECT convert(sum(position), '{currency}') AS v "
                        f"WHERE account ~ '^(Assets|Liabilities)' "
                        f"AND date <= {d.isoformat()}"
                    )
                    rows = await self._query(client, slug, bql)
                    value = Decimal(0)
                    if rows and rows[0]:
                        inv = rows[0][0]
                        if isinstance(inv, dict):
                            value = _dec(inv.get(currency, 0))
                    return NetWorthPoint(date=d, value=value, currency=currency)

                return list(await asyncio.gather(*(point(d) for d in ends)))

        return await self._cached(f"networth:{currency}:{months}", NetWorthPoint, build)

    async def transactions(
        self, account: str | None, frm: date | None, to: date | None
    ) -> list[LedgerTransaction]:
        async def build() -> list[LedgerTransaction]:
            clauses = []
            if account:
                # Allowlist-validated (no regex metachars), then anchored as a
                # prefix match so the client cannot inject BQL/regex.
                safe = _validate_account(account)
                clauses.append(f"account ~ '^{safe}'")
            if frm:
                clauses.append(f"date >= {frm.isoformat()}")
            if to:
                clauses.append(f"date <= {to.isoformat()}")
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            bql = (
                "SELECT id, date, payee, narration, account, number, currency"
                f"{where} ORDER BY date"
            )
            async with httpx.AsyncClient(timeout=20) as client:
                slug = await self._slug(client)
                rows = await self._query(client, slug, bql)
            return _group_transactions(rows)

        key = f"txns:{account or '*'}:{frm or '*'}:{to or '*'}"
        return await self._cached(key, LedgerTransaction, build)

    async def currencies(self, currency: str) -> list[CurrencyTotal]:
        currency = _validate_currency(currency or self._settings.reporting_currency)

        async def build() -> list[CurrencyTotal]:
            bql = (
                "SELECT currency, sum(number) AS units, "
                f"convert(sum(position), '{currency}') AS val "
                "WHERE account ~ '^Assets' GROUP BY currency ORDER BY currency"
            )
            async with httpx.AsyncClient(timeout=15) as client:
                slug = await self._slug(client)
                rows = await self._query(client, slug, bql)
            totals: list[CurrencyTotal] = []
            for row in rows:
                ccy = row[0]
                units = _dec(row[1]) if len(row) > 1 and row[1] is not None else Decimal(0)
                val = Decimal(0)
                if len(row) > 2 and isinstance(row[2], dict):
                    val = _dec(row[2].get(currency, 0))
                totals.append(
                    CurrencyTotal(
                        currency=ccy,
                        units=units,
                        value=val,
                        reporting_currency=currency,
                    )
                )
            return totals

        return await self._cached(f"currencies:{currency}", CurrencyTotal, build)

    async def cash(self, currency: str | None = None) -> Decimal:
        """Liquid ledger cash (bank + wallet) converted to `currency`. Folded into the
        Wealthfolio net worth as the cash WF doesn't track. Excludes
        Assets:Investments / Assets:Crypto — those mirror Wealthfolio (double-count)."""
        currency = _validate_currency(currency or self._settings.reporting_currency)
        bql = (
            f"SELECT convert(sum(position), '{currency}') AS v "
            "WHERE account ~ '^Assets:(Bank|Cash)'"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            slug = await self._slug(client)
            rows = await self._query(client, slug, bql)
        if rows and rows[0]:
            inv = rows[0][0]
            if isinstance(inv, dict):
                return _dec(inv.get(currency, 0))
        return Decimal(0)

    # ── tree builder ─────────────────────────────────────────
    @staticmethod
    def _build_tree(rows: list) -> list[AccountNode]:
        nodes: dict[str, AccountNode] = {}
        tops: list[AccountNode] = []

        def ensure(path: str) -> AccountNode:
            node = nodes.get(path)
            if node is not None:
                return node
            node = AccountNode(name=path, balances={}, children=[])
            nodes[path] = node
            if ":" in path:
                ensure(path.rsplit(":", 1)[0]).children.append(node)
            else:
                tops.append(node)
            return node

        for row in rows:
            acct, bal = row[0], row[1]
            node = ensure(acct)
            if isinstance(bal, dict):
                for ccy, amt in bal.items():
                    node.balances[ccy] = node.balances.get(ccy, Decimal(0)) + _dec(amt)

        # Roll balances up to ancestors (deepest first → each node is complete
        # before it contributes to its parent).
        for path in sorted(nodes, key=lambda p: p.count(":"), reverse=True):
            if ":" in path:
                parent = nodes[path.rsplit(":", 1)[0]]
                for ccy, amt in nodes[path].balances.items():
                    parent.balances[ccy] = parent.balances.get(ccy, Decimal(0)) + amt
        return tops


def _month_ends(months: int) -> list[date]:
    today = datetime.now(timezone.utc).date()
    ends: list[date] = []
    y, m = today.year, today.month
    for _ in range(max(1, months)):
        nexty, nextm = (y + 1, 1) if m == 12 else (y, m + 1)
        last = date(nexty, nextm, 1) - timedelta(days=1)
        ends.append(min(last, today))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return sorted(set(ends))


def _group_transactions(rows: list) -> list[LedgerTransaction]:
    # rows: [id, date, payee, narration, account, number, currency]
    by_id: dict[str, LedgerTransaction] = {}
    order: list[str] = []
    for row in rows:
        tid, d, payee, narration, account, number, currency = (
            row + [None] * 7
        )[:7]
        if tid not in by_id:
            by_id[tid] = LedgerTransaction(
                date=_as_date(d),
                payee=payee,
                narration=narration or "",
                postings=[],
            )
            order.append(tid)
        if account is not None and number is not None:
            by_id[tid].postings.append(
                Posting(
                    account=account,
                    amount=_dec(number),
                    currency=currency or "",
                )
            )
    return [by_id[i] for i in order]


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class DemoLedgerBridge(LedgerBridge):
    """DEMO_MODE bridge: synthetic fixtures from `services/demo.py`, no network
    or Redis touched.

    `query_rows()` (the raw-BQL passthrough) dispatches on the BQL shape via
    `services/demo.ledger_query_rows`: `services/portfolio.py`'s
    ledger-lot lens (`Assets:`-prefixed GROUP BY) stays unmatched -> `[]` —
    this fixture tracks cash only (investment lots live in the Wealthfolio
    fixture), so an empty passthrough there is still accurate and leak-free —
    but `AdvisorEngine`'s three narrower query shapes (trailing-12mo Expenses
    aggregate, clearing-account balance, last-transaction date) now resolve
    against the seed transactions, so the demo life floor is nonzero.
    """

    async def accounts(self) -> list[AccountNode]:
        return self._build_tree(ledger_accounts_rows())

    async def networth(self, currency: str, months: int) -> list[NetWorthPoint]:
        currency = _validate_currency(currency or self._settings.reporting_currency)
        return [
            NetWorthPoint(date=d, value=ledger_value_asof(d, currency), currency=currency)
            for d in _month_ends(months)
        ]

    async def transactions(
        self, account: str | None, frm: date | None, to: date | None
    ) -> list[LedgerTransaction]:
        if account:
            _validate_account(account)
        out: list[LedgerTransaction] = []
        for t in ledger_transactions():
            if frm and t.date < frm:
                continue
            if to and t.date > to:
                continue
            postings = [p for p in t.postings if not account or p.account.startswith(account)]
            if account and not postings:
                continue
            out.append(LedgerTransaction(
                date=t.date, payee=t.payee, narration=t.narration,
                postings=[Posting(account=p.account, amount=p.amount, currency=p.currency) for p in postings],
            ))
        return sorted(out, key=lambda t: t.date)

    async def currencies(self, currency: str) -> list[CurrencyTotal]:
        currency = _validate_currency(currency or self._settings.reporting_currency)
        return [CurrencyTotal(**row) for row in ledger_currencies(currency)]

    async def cash(self, currency: str | None = None) -> Decimal:
        currency = _validate_currency(currency or self._settings.reporting_currency)
        return ledger_cash(currency)

    async def query_rows(self, bql: str) -> list:
        return ledger_query_rows(bql, self._settings.reporting_currency.upper())


def make_ledger(redis: Redis, settings: Settings) -> LedgerBridge:
    """The ONE construction seam for `LedgerBridge` — every call site MUST go
    through this (never `LedgerBridge(...)` directly), or DEMO_MODE leaks the
    real ledger. Returns the demo bridge when `settings.demo_mode`."""
    if settings.demo_mode:
        return DemoLedgerBridge(redis, settings)
    return LedgerBridge(redis, settings)
