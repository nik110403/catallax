"""Pure/instance-level tests for RiskEngine.recompute_with_weights.

No live fetches: synthetic daily price fixtures stand in for the series bundle()
would have fetched, injected directly into the private `_context` cache that
recompute_with_weights() reuses instead of a second rate-limited fetch. The price
walks are seeded (numpy Generator with a fixed seed), not RNG-at-test-time — the
whole compute path (_compute -> _var_block) is deterministic math, so identical
input must produce byte-identical output.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import numpy as np

from app.services.risk import RiskEngine


def _bdays(start: date, n: int) -> list[str]:
    out: list[str] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _walk(n: int, sigma: float, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, sigma, n)
    return [float(x) for x in 100.0 * np.cumprod(1.0 + rets)]


def _flat(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


N = 260  # ~1y business days -> enough weekly windows to exercise the resample
DATES = _bdays(date(2023, 1, 2), N)


def _make_ctx(symbols: dict[str, list[float]]) -> dict[str, Any]:
    resolved = [
        {"wf": sym, "asset_class": "EQUITY", "value": 0.0, "skip": None,
         "price_symbol": sym, "source": "fixture", "currency": "EUR", "currency_source": "mapped"}
        for sym in symbols
    ]
    series: dict[str, Any] = {f"A:{sym}": (DATES, px, False) for sym, px in symbols.items()}
    bench = next(iter(symbols.values()))
    series["B:EQ"] = (DATES, bench, False)
    series["B:CR"] = (DATES, bench, False)
    params = {
        "min_history": 20, "trading_days": 252, "var_conf": 0.95, "var_min": 10,
        "ffill": 3, "lookback": N, "bench_label": "fixture",
    }
    return {"resolved": resolved, "series": series, "cash": 0.0, "total": 100_000.0, "params": params}


def _engine_with_ctx(ctx: dict[str, Any]) -> RiskEngine:
    eng = RiskEngine.__new__(RiskEngine)  # bypass __init__: no redis/settings/wf needed
    eng._context = ctx
    return eng


def test_recompute_with_weights_is_deterministic():
    ctx = _make_ctx({"A": _walk(N, 0.02, seed=1), "B": _walk(N, 0.01, seed=2)})
    eng = _engine_with_ctx(ctx)
    weights = {"A": 0.6, "B": 0.4}
    r1, meta1 = asyncio.run(eng.recompute_with_weights(weights))
    r2, meta2 = asyncio.run(eng.recompute_with_weights(weights))
    assert r1.cvar_pct is not None
    assert r1.horizon_days == 7
    # as_of is a wall-clock stamp, not part of the deterministic risk math.
    d1, d2 = r1.model_dump(), r2.model_dump()
    d1.pop("as_of"), d2.pop("as_of")
    assert d1 == d2
    assert meta1["frequency"] == meta2["frequency"] == "weekly"


def test_trimmed_weights_lower_or_equal_cvar():
    # A = concentrated high-vol asset, B = near-flat low-vol asset.
    ctx = _make_ctx({"A": _walk(N, 0.04, seed=3), "B": _flat(N)})
    eng = _engine_with_ctx(ctx)
    concentrated, _ = asyncio.run(eng.recompute_with_weights({"A": 1.0}))
    trimmed, _ = asyncio.run(eng.recompute_with_weights({"A": 0.3, "B": 0.7}))
    assert concentrated.cvar_pct is not None and trimmed.cvar_pct is not None
    assert trimmed.cvar_pct <= concentrated.cvar_pct


def test_unresolvable_weight_symbol_is_dropped_and_warned():
    ctx = _make_ctx({"A": _walk(N, 0.02, seed=4)})
    eng = _engine_with_ctx(ctx)
    report, meta = asyncio.run(eng.recompute_with_weights({"A": 0.5, "ZZZ": 0.5}))
    assert report.cvar_pct is not None
    assert any("ZZZ" in w for w in meta["warnings"])


def test_overallocated_weights_are_floored_and_warned():
    # sum(weights) > 1.0: cash floors at 0 instead of going negative — must be
    # surfaced as a warning, not silently swallowed (review finding 1).
    ctx = _make_ctx({"A": _walk(N, 0.02, seed=7), "B": _walk(N, 0.01, seed=8)})
    eng = _engine_with_ctx(ctx)
    report, meta = asyncio.run(eng.recompute_with_weights({"A": 0.7, "B": 0.7}))
    assert report.cvar_pct is not None
    assert any("sum to" in w and "1.0" in w for w in meta["warnings"])


def test_daily_frequency_sets_horizon_one():
    ctx = _make_ctx({"A": _walk(N, 0.02, seed=5), "B": _walk(N, 0.01, seed=6)})
    eng = _engine_with_ctx(ctx)
    report, _ = asyncio.run(eng.recompute_with_weights({"A": 1.0}, frequency="daily"))
    assert report.horizon_days == 1
