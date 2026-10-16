import math
from dataclasses import dataclass
from datetime import date

import pytest
from app.services.algo import (
    fill_price,
    limit_crosses,
    new_avg_cost,
    notional_ok,
    pick_day_pnl_baseline,
    replay_cash,
)


def test_fill_price_adverse_both_sides():
    assert fill_price(100.0, "buy", 5.0) == pytest.approx(100.05)
    assert fill_price(100.0, "sell", 5.0) == pytest.approx(99.95)
    assert fill_price(100.0, "buy", 0.0) == 100.0


def test_new_avg_cost_weighted():
    assert new_avg_cost(0, 0.0, 10, 50.0) == pytest.approx(50.0)
    assert new_avg_cost(10, 50.0, 10, 60.0) == pytest.approx(55.0)


def test_limit_crosses():
    assert limit_crosses("buy", 99.0, 100.0) is True      # quote under limit -> buy fills
    assert limit_crosses("buy", 101.0, 100.0) is False
    assert limit_crosses("sell", 101.0, 100.0) is True    # quote over limit -> sell fills
    assert limit_crosses("sell", 99.0, 100.0) is False
    assert limit_crosses("buy", 100.0, 100.0) is True     # at limit fills


@dataclass
class _FakeSnapshot:
    snapshot_date: date
    equity: float


def test_pick_day_pnl_baseline_skips_todays_own_snapshot():
    today = date(2026, 7, 13)
    snapshots_desc = [
        _FakeSnapshot(today, 1100.0),                         # today's own snapshot — must be skipped
        _FakeSnapshot(date(2026, 7, 12), 1000.0),              # prior close — this is the baseline
        _FakeSnapshot(date(2026, 7, 11), 900.0),
    ]
    baseline = pick_day_pnl_baseline(snapshots_desc, today)
    assert baseline is not None
    assert baseline.equity == 1000.0


def test_pick_day_pnl_baseline_none_when_no_prior_day():
    today = date(2026, 7, 13)
    snapshots_desc = [_FakeSnapshot(today, 1100.0)]
    assert pick_day_pnl_baseline(snapshots_desc, today) is None


def test_pick_day_pnl_baseline_empty_list():
    assert pick_day_pnl_baseline([], date(2026, 7, 13)) is None


def test_order_spec_validation():
    from app.services.algo import OrderSpec
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="buy", qty=-5)
    with pytest.raises(Exception):
        OrderSpec(symbol="ba;d", side="buy", qty=1)
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="buy", type="limit", qty=1)  # limit needs limit_price
    s = OrderSpec(symbol="aapl", side="buy", qty=1)
    assert s.symbol == "AAPL"


def test_order_spec_rejects_non_finite_qty():
    from app.services.algo import OrderSpec
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="buy", qty=float("nan"))
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="buy", qty=float("inf"))
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="buy", qty=-1)


def test_order_spec_rejects_bad_limit_price():
    from app.services.algo import OrderSpec
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="sell", type="limit", qty=1, limit_price=-5)
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="sell", type="limit", qty=1, limit_price=float("nan"))
    with pytest.raises(Exception):
        OrderSpec(symbol="AAPL", side="sell", type="limit", qty=1, limit_price=0)
    s = OrderSpec(symbol="AAPL", side="sell", type="limit", qty=1, limit_price=100.0)
    assert s.limit_price == 100.0


def test_notional_ok():
    assert notional_ok(10, 100.0, 25000.0) is True       # 1000 <= 25000
    assert notional_ok(300, 100.0, 25000.0) is False      # 30000 > 25000
    assert notional_ok(250, 100.0, 25000.0) is True       # exactly at cap


@dataclass
class _FakeFill:
    side: str
    qty: float
    price: float
    commission: float


def test_replay_cash_matches_hand_computation():
    fills = [
        _FakeFill("buy", 10, 50.0, 1.0),    # -500 - 1
        _FakeFill("sell", 5, 60.0, 1.0),    # +300 - 1
        _FakeFill("buy", 2, 10.0, 1.0),     # -20 - 1
    ]
    # starting 1000: 1000 - 501 + 299 - 21 = 777
    assert replay_cash(fills, 1000.0) == pytest.approx(777.0)


def test_replay_cash_no_fills_returns_starting_cash():
    assert replay_cash([], 100000.0) == pytest.approx(100000.0)
