"""DEMO_MODE fixtures (Phase 14a) — a single fictional persona, no reference to
a real person, accounts, or balances.

Deterministic (`random.Random(20260720)`) but date-relative to `date.today()` so
the demo always looks current. Consumed by `DemoWealthfolioBridge`
(`services/wealthfolio.py`) and `DemoLedgerBridge` (`services/ledger.py`) — this
module holds pure fixture data/functions only, no bridge classes, so it has zero
dependency on either (avoids a circular import; those two modules import from
here, never the other way round).

Wealthfolio (the portfolio source): 7 holdings + cash, target total ~EUR 48,500.
`wf_networth_series()` is rescaled so its last point lands exactly on
`WF_TOTAL` — the Home hero figure and the Wealthfolio Holdings total therefore
agree *for the Wealthfolio-only figures*. The composed `/api/v1/networth`
total additionally folds in the demo ledger's own (small, non-zero) cash
figure via the unmodified `combined()`/`networth()` call chain — exactly like
production folds in bank cash on top of the Wealthfolio total.
That composed figure is therefore slightly ABOVE EUR 48,500 by design, not a
bug: only the Wealthfolio-internal invariant (this module's job) is exact.

Ledger (the "sample ledger"): cash-only accounts tree + ~20 plausible
transactions, mirroring the intended setup (investment lots live
in Wealthfolio, not here).
"""
from __future__ import annotations

import math
import random
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

_SEED = 20260720
# Fixed illustrative FX rate — demo fixtures don't need live conversion.
_USD_EUR = Decimal("0.92")


def _fx(ccy: str, target: str) -> Decimal:
    if ccy == target:
        return Decimal("1")
    if ccy == "USD" and target == "EUR":
        return _USD_EUR
    if ccy == "EUR" and target == "USD":
        return Decimal("1") / _USD_EUR
    return Decimal("1")  # unmapped pair: identity fallback (no live FX in demo fixtures)


# ── Wealthfolio (the demo persona's invested portfolio) ────────────────────
class DemoAsset(NamedTuple):
    symbol: str
    name: str
    asset_class: str  # "equity" | "crypto" — matches Allocation.tsx's CLASS_COLORS keys
    quantity: Decimal
    value: Decimal  # EUR
    # Synthetic cost basis (SE4) — mirrors the exporter's `/holdings.by_asset[].cost_basis`
    # (SE2), a mix of gains and one loss so the advisor's gain-trigger tax leg
    # exercises both the real-basis path and the floor-at-0 (loss) branch in demo mode.
    cost_basis: Decimal = Decimal("0")


WF_ASSETS: list[DemoAsset] = [
    DemoAsset("IWDA.AS", "iShares Core MSCI World UCITS ETF", "equity", Decimal("142"), Decimal("18420"), Decimal("15200")),
    DemoAsset("VWCE.DE", "Vanguard FTSE All-World UCITS ETF", "equity", Decimal("61"), Decimal("7950"), Decimal("6800")),
    DemoAsset("AAPL", "Apple Inc.", "equity", Decimal("22"), Decimal("6580"), Decimal("7100")),  # unrealised loss
    DemoAsset("BTC", "Bitcoin", "crypto", Decimal("0.21"), Decimal("5940"), Decimal("2100")),
    DemoAsset("MSFT", "Microsoft Corp.", "equity", Decimal("9"), Decimal("3180"), Decimal("2500")),
    DemoAsset("ETH", "Ethereum", "crypto", Decimal("1.40"), Decimal("2340"), Decimal("2600")),  # unrealised loss
    DemoAsset("NVDA", "NVIDIA Corp.", "equity", Decimal("12"), Decimal("2190"), Decimal("900")),
]
WF_CASH = Decimal("1900")
WF_INVESTMENTS = sum((a.value for a in WF_ASSETS), Decimal("0"))
WF_TOTAL = WF_INVESTMENTS + WF_CASH  # == 48500 exactly

# Which Wealthfolio account holds which symbols (by_account breakdown).
_WF_ACCOUNT_SYMBOLS: dict[str, set[str]] = {
    "Broker": {"IWDA.AS", "VWCE.DE", "AAPL", "MSFT", "NVDA"},
    "Crypto Exchange": {"BTC", "ETH"},
}


def wf_accounts() -> list[dict]:
    """HoldingAccount rows: Broker (equities/ETFs), Crypto Exchange, Demo Bank (cash)."""
    rows: list[dict] = []
    for account, symbols in _WF_ACCOUNT_SYMBOLS.items():
        inv = sum((a.value for a in WF_ASSETS if a.symbol in symbols), Decimal("0"))
        rows.append({"account": account, "value": inv, "investments": inv, "cash": Decimal("0")})
    rows.append({"account": "Demo Bank", "value": WF_CASH, "investments": Decimal("0"), "cash": WF_CASH})
    return rows


def wf_networth_series(months: int) -> list[tuple[date, Decimal, Decimal, Decimal]]:
    """One point per day for the requested window: a seeded random walk with mild
    upward drift and two visible, smoothly-recovering drawdowns, rescaled so the
    LAST point (today) equals WF_TOTAL exactly.

    Each point is ``(date, total_value, cash, investments)`` — a synthetic
    cash/investments split mirroring the real exporter's per-day breakdown. Cash
    is held constant at ``WF_CASH``; investments is the remainder (floored at 0,
    though the series never dips low enough to hit that floor)."""
    rng = random.Random(_SEED)
    today = date.today()
    days = max(60, months * 30)
    start_day = today - timedelta(days=days)
    n = (today - start_day).days + 1

    target = float(WF_TOTAL)
    base = target * 0.72  # ~28% cumulative growth over the window (mild upward drift)
    drift = math.log(target / base) / (n - 1)
    vals = [base]
    for _ in range(1, n):
        vals.append(vals[-1] * math.exp(drift + rng.gauss(0.0, 0.007)))

    # Two visible drawdowns: a parabolic dip that returns to 1.0 at its own
    # window edges, so it doesn't disturb the overall drift/rescale.
    for center_frac, depth, width_frac in ((0.30, 0.11, 0.09), (0.68, 0.08, 0.07)):
        center = int(n * center_frac)
        width = max(3, int(n * width_frac))
        for i in range(max(0, center - width), min(n, center + width)):
            t = (i - center) / width
            vals[i] *= 1 - depth * max(0.0, 1 - t * t)

    scale = target / vals[-1]
    out: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for i, v in enumerate(vals):
        v = target if i == n - 1 else v * scale  # pin the last point exactly (kill float residue)
        total = Decimal(str(round(v, 2)))
        investments = max(total - WF_CASH, Decimal("0"))
        out.append((start_day + timedelta(days=i), total, WF_CASH, investments))
    return out


def wf_position_history(symbol: str, months: int) -> list[tuple[date, Decimal, Decimal, Decimal]] | None:
    """Synthetic per-symbol positions-history (SE4's demo counterpart to the
    exporter's `/positions-history`, SE3) — a seeded random walk scaled to
    that `WF_ASSETS` entry, quantity held constant (no synthetic buys/sells),
    value/cost_basis each pinned to the asset's current figures on the LAST
    point (mirrors `wf_networth_series`' pin to `WF_TOTAL`). Cost basis ramps
    linearly from 85% of its final value to the pinned figure — a visible,
    deterministic unrealised-gain (or, for AAPL/ETH, loss) story across the
    window. Returns `None` for a symbol not in `WF_ASSETS`."""
    asset = next((a for a in WF_ASSETS if a.symbol == symbol.upper()), None)
    if asset is None:
        return None
    rng = random.Random(_SEED + sum(ord(c) for c in asset.symbol))
    today = date.today()
    days = max(60, months * 30)
    start_day = today - timedelta(days=days)
    n = (today - start_day).days + 1

    target = float(asset.value)
    base = target * 0.55
    drift = math.log(target / base) / (n - 1)
    vals = [base]
    for _ in range(1, n):
        vals.append(vals[-1] * math.exp(drift + rng.gauss(0.0, 0.01)))
    scale = target / vals[-1]

    cb_target = float(asset.cost_basis)
    cb_start = cb_target * 0.85

    out: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for i, v in enumerate(vals):
        v = target if i == n - 1 else v * scale
        frac = i / (n - 1) if n > 1 else 1.0
        cb = cb_target if i == n - 1 else cb_start + (cb_target - cb_start) * frac
        out.append((
            start_day + timedelta(days=i), asset.quantity,
            Decimal(str(round(v, 2))), Decimal(str(round(cb, 2))),
        ))
    return out


# ── Ledger (the sample cash ledger — investment lots live in Wealthfolio) ──
class DemoPosting(NamedTuple):
    account: str
    amount: Decimal
    currency: str


class DemoTxn(NamedTuple):
    date: date
    payee: str | None
    narration: str
    postings: list[DemoPosting]


# (days_ago, payee, narration, [(account, amount, currency), ...]) — postings
# balance to zero per currency across the whole set (verified by hand; every
# leaf account below is derived from this single source of truth).
_TXN_SPECS: list[tuple[int, str | None, str, list[tuple[str, str, str]]]] = [
    (58, "Employer Ltd", "Monthly salary", [("Income:Salary", "-3200.00", "EUR"), ("Assets:Bank:Checking", "3200.00", "EUR")]),
    (55, "City Property Management", "Rent", [("Expenses:Rent", "950.00", "EUR"), ("Assets:Bank:Checking", "-950.00", "EUR")]),
    (53, "Supermarket", "Groceries", [("Expenses:Groceries", "62.40", "EUR"), ("Assets:Bank:Checking", "-62.40", "EUR")]),
    (52, "ATM Withdrawal", "Cash withdrawal", [("Assets:Cash:Wallet", "150.00", "EUR"), ("Assets:Bank:Checking", "-150.00", "EUR")]),
    (50, "City Transit", "Monthly transport pass", [("Expenses:Transport", "89.00", "EUR"), ("Assets:Bank:Checking", "-89.00", "EUR")]),
    (48, "Supermarket", "Groceries", [("Expenses:Groceries", "47.85", "EUR"), ("Assets:Cash:Wallet", "-47.85", "EUR")]),
    (45, "Cinema", "Cinema night", [("Expenses:Leisure", "28.00", "EUR"), ("Assets:Cash:Wallet", "-28.00", "EUR")]),
    (44, "Overseas Client", "Freelance invoice payment", [("Income:Salary", "-180.00", "USD"), ("Assets:Bank:Savings", "180.00", "USD")]),
    (42, "Netflix", "Streaming subscription", [("Expenses:Leisure", "14.99", "USD"), ("Liabilities:CreditCard", "-14.99", "USD")]),
    (40, "Grocery Store", "Groceries", [("Expenses:Groceries", "53.10", "EUR"), ("Assets:Bank:Checking", "-53.10", "EUR")]),
    (37, "Uber", "Ride", [("Expenses:Transport", "18.50", "EUR"), ("Liabilities:CreditCard", "-18.50", "EUR")]),
    (35, None, "Monthly savings transfer", [("Assets:Bank:Savings", "500.00", "EUR"), ("Assets:Bank:Checking", "-500.00", "EUR")]),
    (33, "Supermarket", "Groceries", [("Expenses:Groceries", "58.20", "EUR"), ("Assets:Bank:Checking", "-58.20", "EUR")]),
    (30, "Employer Ltd", "Monthly salary", [("Income:Salary", "-3200.00", "EUR"), ("Assets:Bank:Checking", "3200.00", "EUR")]),
    (28, "City Property Management", "Rent", [("Expenses:Rent", "950.00", "EUR"), ("Assets:Bank:Checking", "-950.00", "EUR")]),
    (26, "Grocery Store", "Groceries", [("Expenses:Groceries", "44.65", "EUR"), ("Assets:Cash:Wallet", "-44.65", "EUR")]),
    (25, "ATM Withdrawal", "Cash withdrawal", [("Assets:Cash:Wallet", "150.00", "EUR"), ("Assets:Bank:Checking", "-150.00", "EUR")]),
    (24, "Amazon", "Books", [("Expenses:Leisure", "32.40", "EUR"), ("Liabilities:CreditCard", "-32.40", "EUR")]),
    (21, "City Transit", "Monthly transport pass", [("Expenses:Transport", "89.00", "EUR"), ("Assets:Bank:Checking", "-89.00", "EUR")]),
    (18, "Supermarket", "Groceries", [("Expenses:Groceries", "51.75", "EUR"), ("Assets:Bank:Checking", "-51.75", "EUR")]),
    (15, "Restaurant", "Dinner out", [("Expenses:Leisure", "64.00", "EUR"), ("Liabilities:CreditCard", "-64.00", "EUR")]),
    (12, "Grocery Store", "Groceries", [("Expenses:Groceries", "39.90", "EUR"), ("Assets:Cash:Wallet", "-39.90", "EUR")]),
    (9, "Uber", "Ride", [("Expenses:Transport", "15.20", "EUR"), ("Liabilities:CreditCard", "-15.20", "EUR")]),
    (6, "Supermarket", "Groceries", [("Expenses:Groceries", "49.30", "EUR"), ("Assets:Bank:Checking", "-49.30", "EUR")]),
    (3, "Credit Card Payment", "Pay down card", [("Liabilities:CreditCard", "100.00", "EUR"), ("Assets:Bank:Checking", "-100.00", "EUR")]),
]


def _all_postings() -> list[tuple[str, str, str]]:
    return [p for _, _, _, postings in _TXN_SPECS for p in postings]


def ledger_accounts_rows() -> list[tuple[str, dict[str, Decimal]]]:
    """Leaf account -> {currency: balance}, aggregated from the seed
    transactions — feed straight into `LedgerBridge._build_tree()` for the
    roll-up-to-ancestors math (same code the real bridge uses)."""
    out: dict[str, dict[str, Decimal]] = {}
    for account, amount, currency in _all_postings():
        bucket = out.setdefault(account, {})
        bucket[currency] = bucket.get(currency, Decimal("0")) + Decimal(amount)
    return list(out.items())


def ledger_cash(currency: str = "EUR") -> Decimal:
    """Bank + Cash balance, consistent with `ledger_accounts_rows()` (same
    aggregation, same seed transactions)."""
    total = Decimal("0")
    for account, balances in ledger_accounts_rows():
        if account.startswith("Assets:Bank") or account.startswith("Assets:Cash"):
            for ccy, amt in balances.items():
                total += amt * _fx(ccy, currency)
    return total.quantize(Decimal("0.01"))


def ledger_currencies(currency: str = "EUR") -> list[dict]:
    """EUR majority + a USD slice, across every Assets: leaf."""
    units: dict[str, Decimal] = {}
    for account, balances in ledger_accounts_rows():
        if not account.startswith("Assets"):
            continue
        for ccy, amt in balances.items():
            units[ccy] = units.get(ccy, Decimal("0")) + amt
    return [
        {
            "currency": ccy,
            "units": units[ccy],
            "value": (units[ccy] * _fx(ccy, currency)).quantize(Decimal("0.01")),
            "reporting_currency": currency,
        }
        for ccy in sorted(units)
    ]


def ledger_value_asof(cutoff: date, currency: str = "EUR") -> Decimal:
    """Assets+Liabilities balance as of `cutoff` — a point-in-time reconstruction
    from the same seed transactions (illustrative fixed FX for the USD leg)."""
    today = date.today()
    total = Decimal("0")
    for days_ago, _, _, postings in _TXN_SPECS:
        if today - timedelta(days=days_ago) > cutoff:
            continue
        for account, amount, ccy in postings:
            if account.startswith("Assets") or account.startswith("Liabilities"):
                total += Decimal(amount) * _fx(ccy, currency)
    return total.quantize(Decimal("0.01"))


_LEDGER_CLEARING_RE = re.compile(r"account ~ '\^([^']+)'$")
_LEDGER_EXPENSES_RE = re.compile(
    r"account ~ '\^Expenses' AND date >= (\d{4}-\d{2}-\d{2}) AND date <= (\d{4}-\d{2}-\d{2})$"
)


def ledger_query_rows(bql: str, currency: str = "EUR") -> list:
    """Minimal BQL-shape dispatch for the three query
    patterns `AdvisorEngine` issues (`_trailing_12mo_avg_monthly_expenses`,
    `_clearing_balance`, `_ledger_staleness_days`) — NOT a general BQL
    engine, and deliberately narrow: `SELECT max(date) AS d`, or a single
    `convert(sum(position), ...)` column whose WHERE clause ENDS at
    `account ~ '^prefix'` (optionally with the exact trailing
    `AND date >= ... AND date <= ...` range), end-anchored (`$`) on
    purpose. `services/portfolio.py` issues a structurally near-identical
    `account ~ '^Assets:' AND (...)` shape for its own cash/holdings
    queries, but always with more conjuncts or a `GROUP BY` appended after
    the account clause — the end-anchor is what keeps this dispatch from
    also matching (and fabricating) THAT query: the ledger fixture holds
    no investment lots (those live in the Wealthfolio fixture), so
    `portfolio.py`'s queries must keep falling through to `[]` unmatched.
    Dispatch order mirrors `tests/test_advisor.py`'s hand-rolled `FakeLedger`.

    Feeding the demo life floor (design acceptance criterion 1): the
    Expenses-prefixed query now sums the seed transactions' real Expenses:*
    postings instead of returning [], so `advisor.py`'s
    `ADVISOR_LIFE_FLOOR_MULTIPLIER × trailing-12mo avg monthly Expenses` is
    nonzero in demo mode.
    """
    if "max(date)" in bql:
        dates = [date.today() - timedelta(days=days_ago) for days_ago, *_ in _TXN_SPECS]
        return [[max(dates).isoformat()]] if dates else [[None]]

    date_from = date_to = None
    em = _LEDGER_EXPENSES_RE.search(bql)
    if em:
        prefix = "Expenses"
        date_from, date_to = date.fromisoformat(em.group(1)), date.fromisoformat(em.group(2))
    else:
        cm = _LEDGER_CLEARING_RE.search(bql)
        if cm is None:
            return []
        prefix = cm.group(1)

    today = date.today()
    total = Decimal("0")
    for days_ago, _, _, postings in _TXN_SPECS:
        d = today - timedelta(days=days_ago)
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        for account, amount, ccy in postings:
            if account.startswith(prefix):
                total += Decimal(amount) * _fx(ccy, currency)
    return [[{currency: total.quantize(Decimal("0.01"))}]]


def ledger_transactions() -> list[DemoTxn]:
    """The seed transactions with dates resolved against today (called fresh on
    every request, never cached at import time, so they stay "recent")."""
    today = date.today()
    return [
        DemoTxn(
            date=today - timedelta(days=days_ago),
            payee=payee,
            narration=narration,
            postings=[DemoPosting(account=a, amount=Decimal(amt), currency=c) for a, amt, c in postings],
        )
        for days_ago, payee, narration, postings in _TXN_SPECS
    ]
