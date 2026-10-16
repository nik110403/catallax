from datetime import datetime, timezone

from app.services.strategy_runner import cap_orders, diff_to_orders, rebalance_due, target_weights


def test_target_weights_equal_capped():
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.5}
    w = target_weights(scores, top_n=2, weighting="equal", max_position_pct=0.25)
    assert set(w) == {"A", "B"} and all(abs(v - 0.25) < 1e-9 for v in w.values())


def test_target_weights_score_ordering():
    scores = {"A": 3.0, "B": 1.0, "C": 2.0}
    w = target_weights(scores, top_n=3, weighting="score", max_position_pct=1.0)
    assert w["A"] > w["C"] > w["B"] and abs(sum(w.values()) - 1.0) < 1e-6


def test_diff_to_orders_sells_first_and_skips_dust():
    orders = diff_to_orders(
        targets={"A": 0.5}, positions={"B": 10.0}, marks={"A": 100.0, "B": 20.0}, equity=1000.0
    )
    assert orders[0] == ("B", "sell", 10.0)           # exit non-target first
    assert ("A", "buy", 5.0) in orders                # 0.5*1000/100
    tiny = diff_to_orders(targets={"A": 0.001}, positions={}, marks={"A": 100.0}, equity=1000.0)
    assert tiny == []                                  # < min_notional skipped


def test_target_weights_all_equal_scores_epsilon_fallback():
    # "score" weighting with every score identical collapses to the epsilon
    # fallback in the numerator — should still land at ~1/n each, not blow up.
    scores = {"A": 5.0, "B": 5.0, "C": 5.0}
    w = target_weights(scores, top_n=3, weighting="score", max_position_pct=1.0)
    assert set(w) == {"A", "B", "C"}
    for v in w.values():
        assert abs(v - (1.0 / 3.0)) < 1e-6


def test_target_weights_cap_redistribution_stays_within_cap():
    # A dominates the score-weighted raw split; capping it at 0.4 and
    # redistributing the excess across B/C must never push them back over
    # the same cap (the redistribution's own min() re-clamp is what's tested).
    scores = {"A": 10.0, "B": 1.0, "C": 1.0}
    w = target_weights(scores, top_n=3, weighting="score", max_position_pct=0.4)
    assert w["A"] == 0.4
    assert all(v <= 0.4 + 1e-9 for v in w.values())
    assert sum(w.values()) <= 1.0 + 1e-9


def test_target_weights_top_n_larger_than_scores():
    scores = {"A": 1.0, "B": 2.0}
    w = target_weights(scores, top_n=5, weighting="equal", max_position_pct=1.0)
    assert set(w) == {"A", "B"}
    assert all(abs(v - 0.5) < 1e-9 for v in w.values())


def test_diff_to_orders_partial_adjust_both_directions():
    # A: held 1.0, target implies 3.0 -> buy 2.0. B: held 5.0, target implies
    # 1.0 -> sell 4.0. Both symbols appear in both positions and targets.
    orders = diff_to_orders(
        targets={"A": 0.3, "B": 0.1},
        positions={"A": 1.0, "B": 5.0},
        marks={"A": 100.0, "B": 100.0},
        equity=1000.0,
    )
    assert ("A", "buy", 2.0) in orders
    assert ("B", "sell", 4.0) in orders


def test_diff_to_orders_missing_mark_skipped_no_raise():
    orders = diff_to_orders(
        targets={"A": 0.5}, positions={"C": 10.0}, marks={"A": 100.0}, equity=1000.0
    )
    symbols = {o[0] for o in orders}
    assert "C" not in symbols  # no mark for C -> skipped, never raised


def test_diff_to_orders_zero_mark_skipped():
    orders = diff_to_orders(
        targets={"A": 0.5, "D": 0.5},
        positions={},
        marks={"A": 100.0, "D": 0.0},
        equity=1000.0,
    )
    symbols = {o[0] for o in orders}
    assert "D" not in symbols  # zero mark -> skipped, no ZeroDivisionError
    assert "A" in symbols


def test_diff_to_orders_full_exit_when_target_absent():
    orders = diff_to_orders(
        targets={}, positions={"E": 7.0}, marks={"E": 50.0}, equity=1000.0
    )
    assert orders == [("E", "sell", 7.0)]


def test_cap_orders_sells_never_truncated_buys_are():
    sells = [("S1", "sell", 1.0), ("S2", "sell", 1.0), ("S3", "sell", 1.0)]
    buys = [("B1", "buy", 1.0), ("B2", "buy", 1.0), ("B3", "buy", 1.0),
            ("B4", "buy", 1.0), ("B5", "buy", 1.0)]
    result = cap_orders(sells, buys, max_orders=4)
    result_sells = [o for o in result if o[1] == "sell"]
    result_buys = [o for o in result if o[1] == "buy"]
    assert result_sells == sells                # all sells survive, untouched
    assert result_buys == buys[:1]               # buys truncated to fill the remaining slot


def test_cap_orders_sells_alone_at_or_over_max_emits_no_buys():
    sells = [("S1", "sell", 1.0), ("S2", "sell", 1.0), ("S3", "sell", 1.0), ("S4", "sell", 1.0)]
    buys = [("B1", "buy", 1.0)]
    result = cap_orders(sells, buys, max_orders=3)
    assert result == sells
    assert all(o[1] == "sell" for o in result)


def test_rebalance_due_calendar():
    now = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)   # a Monday
    assert rebalance_due(None, "daily", now) is True
    assert rebalance_due(datetime(2026, 7, 12, 23, 0, tzinfo=timezone.utc), "daily", now) is True
    assert rebalance_due(datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc), "daily", now) is False
    assert rebalance_due(datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc), "weekly", now) is True   # prev ISO week
    assert rebalance_due(datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc), "monthly", now) is True
