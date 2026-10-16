"""yfinance history: NaN/missing-OHLC rows (e.g. today's EU partial row) are dropped, not fatal."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import app.adapters.yfinance_adapter as yfa
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.errors import UpstreamError

_NAN = float("nan")


def _call(monkeypatch: pytest.MonkeyPatch, rows: list[dict]):
    idx = pd.date_range("2026-10-13", periods=len(rows), tz="Europe/Amsterdam")
    frame = pd.DataFrame(rows, index=idx)

    class _T:
        def __init__(self, *_a, **_k): ...
        def history(self, **_k):
            return frame

    monkeypatch.setattr(yfa.yf, "Ticker", _T)
    a = YFinanceAdapter.__new__(YFinanceAdapter)
    a._yf_symbol = lambda s: s  # type: ignore[method-assign]
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return a._blocking_history("IWDA.AS", t, t, "1d")


def test_nan_row_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _call(monkeypatch, [
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10},
        {"Open": 1.5, "High": 2.5, "Low": 1.0, "Close": 2.0, "Volume": 11},
        {"Open": _NAN, "High": _NAN, "Low": _NAN, "Close": _NAN, "Volume": 0},
    ])
    assert len(out) == 2 and float(out[-1].close) == 2.0


def test_all_nan_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(UpstreamError):
        _call(monkeypatch, [{"Open": _NAN, "High": _NAN, "Low": _NAN, "Close": _NAN, "Volume": 0}])
