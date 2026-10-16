"""Regression tests for the DEMO_MODE construction seam (Phase 14a):
`make_wealthfolio`/`make_ledger` are documented as the ONE construction point
for each bridge (see their docstrings in `services/wealthfolio.py` /
`services/ledger.py`) — every other call site must route through them or
DEMO_MODE leaks a real portfolio/ledger. Pure unit tests: no network,
Postgres, or Redis touched (the demo bridges are fixture-only by design).
"""
from __future__ import annotations

from datetime import date, timedelta

from app.config import Settings
from app.services.advisor import AdvisorEngine, _DemoRegime, _DemoRisk, make_advisor
from app.services.demo import WF_CASH, WF_INVESTMENTS, WF_TOTAL
from app.services.demo import ledger_cash as demo_ledger_cash
from app.services.ledger import DemoLedgerBridge, LedgerBridge, make_ledger
from app.services.market_research import MarketResearchEngine
from app.services.risk import RiskEngine
from app.services.wealthfolio import DemoWealthfolioBridge, WealthfolioBridge, make_wealthfolio


def _settings(**kw: object) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


# ── construction seam: exact class selection ─────────────────────────
def test_make_wealthfolio_demo_on_returns_demo_bridge() -> None:
    bridge = make_wealthfolio(object(), _settings(demo_mode=True))
    assert type(bridge) is DemoWealthfolioBridge


def test_make_wealthfolio_demo_off_returns_base_bridge() -> None:
    bridge = make_wealthfolio(object(), _settings(demo_mode=False))
    assert type(bridge) is WealthfolioBridge


def test_make_ledger_demo_on_returns_demo_bridge() -> None:
    bridge = make_ledger(object(), _settings(demo_mode=True))
    assert type(bridge) is DemoLedgerBridge


def test_make_ledger_demo_off_returns_base_bridge() -> None:
    bridge = make_ledger(object(), _settings(demo_mode=False))
    assert type(bridge) is LedgerBridge


# ── demo fixtures: synthetic data, never the real portfolio/ledger ──
async def test_demo_wealthfolio_holdings_match_fixtures() -> None:
    bridge = make_wealthfolio(object(), _settings(demo_mode=True))
    holdings = await bridge.holdings()
    assert holdings.total == WF_TOTAL
    assert holdings.investments == WF_INVESTMENTS
    assert holdings.cash == WF_CASH
    assert len(holdings.by_asset) == 7  # len(WF_ASSETS)


async def test_demo_wealthfolio_networth_last_point_pins_to_total() -> None:
    bridge = make_wealthfolio(object(), _settings(demo_mode=True))
    points = await bridge.networth(months=1)
    assert points
    assert points[-1].value == WF_TOTAL


async def test_demo_ledger_cash_matches_fixture() -> None:
    bridge = make_ledger(object(), _settings(demo_mode=True))
    cash = await bridge.cash("EUR")
    assert cash == demo_ledger_cash("EUR")


async def test_demo_ledger_query_rows_returns_empty_never_proxies_to_fava() -> None:
    """`query_rows()` (the raw-BQL passthrough the portfolio engine's
    ledger-lot lens calls directly) must return [] in demo mode — never reach
    a real Fava, or the demo seam would fall through to real data via this
    one side door."""
    bridge = make_ledger(object(), _settings(demo_mode=True))
    rows = await bridge.query_rows("SELECT account, sum(position)")
    assert rows == []


async def test_demo_ledger_query_rows_portfolio_lot_lens_still_stays_empty() -> None:
    """Regression guard: `services/portfolio.py` issues an
    `account ~ '^Assets:'`-shaped query too (its own cash/holdings lens) —
    structurally close to `AdvisorEngine`'s clearing-balance query. It MUST
    stay unmatched (the ledger fixture holds no investment lots — those live
    in the Wealthfolio fixture), never start fabricating ledger-side
    positions via the dispatch added for the advisor's own queries."""
    bridge = make_ledger(object(), _settings(demo_mode=True))
    cash_bql = "SELECT convert(sum(position), 'EUR') AS v WHERE account ~ '^Assets:' AND (currency = 'EUR')"
    holdings_bql = (
        "SELECT currency, sum(number) AS qty, convert(cost(sum(position)), 'EUR') AS cost, "
        "convert(value(sum(position)), 'EUR') AS val WHERE account ~ '^Assets:' AND currency != 'EUR' "
        "GROUP BY currency ORDER BY currency"
    )
    assert await bridge.query_rows(cash_bql) == []
    assert await bridge.query_rows(holdings_bql) == []


async def test_demo_ledger_query_rows_trailing_12mo_expenses_nonzero() -> None:
    """Design acceptance criterion 1: the demo life floor must be nonzero.
    `AdvisorEngine._trailing_12mo_avg_monthly_expenses`'s exact BQL shape,
    a window comfortably covering every seed transaction."""
    bridge = make_ledger(object(), _settings(demo_mode=True))
    as_of = date.today()
    start = as_of - timedelta(days=365)
    bql = (
        f"SELECT convert(sum(position), 'EUR') AS v WHERE account ~ '^Expenses' "
        f"AND date >= {start.isoformat()} AND date <= {as_of.isoformat()}"
    )
    rows = await bridge.query_rows(bql)
    assert rows and rows[0][0]["EUR"] > 0


async def test_demo_ledger_query_rows_staleness_returns_a_recent_date() -> None:
    bridge = make_ledger(object(), _settings(demo_mode=True))
    rows = await bridge.query_rows("SELECT max(date) AS d")
    assert rows and rows[0][0] is not None


# ── make_advisor: DEMO_MODE construction seam ─────────────────────
async def test_make_advisor_demo_on_injects_synthetic_zero_io_bridges() -> None:
    """No advisor code path may perform live egress under DEMO_MODE — the
    injected `risk`/`regime` bridges must be the synthetic `_DemoRisk`/
    `_DemoRegime` stand-ins, never the real `RiskEngine`/`MarketResearchEngine`."""
    advisor = make_advisor(object(), _settings(demo_mode=True))
    assert type(advisor) is AdvisorEngine
    assert type(advisor._wf) is DemoWealthfolioBridge  # noqa: SLF001
    assert type(advisor._ledger) is DemoLedgerBridge  # noqa: SLF001
    assert type(advisor._risk) is _DemoRisk  # noqa: SLF001
    assert type(advisor._regime) is _DemoRegime  # noqa: SLF001


async def test_make_advisor_demo_off_injects_real_bridges() -> None:
    advisor = make_advisor(object(), _settings(demo_mode=False))
    assert type(advisor._wf) is WealthfolioBridge  # noqa: SLF001
    assert type(advisor._ledger) is LedgerBridge  # noqa: SLF001
    assert type(advisor._risk) is RiskEngine  # noqa: SLF001
    assert type(advisor._regime) is MarketResearchEngine  # noqa: SLF001


async def test_demo_risk_bundle_and_recompute_touch_no_io() -> None:
    """`_DemoRisk` is a pure function of the weight vector (mirrors
    `tests/test_advisor.py`'s `FakeRisk`): concentration (HHI) raises CVaR,
    so a trim that de-concentrates the book always lowers it."""
    risk = _DemoRisk()
    await risk.bundle()
    concentrated, _ = await risk.recompute_with_weights({"BTC": 0.9, "AAPL": 0.1})
    diversified, _ = await risk.recompute_with_weights({"BTC": 0.5, "AAPL": 0.5})
    assert concentrated.cvar_pct > diversified.cvar_pct
