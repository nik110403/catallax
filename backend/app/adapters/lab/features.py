"""Cross-sectional feature/target panel builder — pure pandas/numpy.

Turns per-symbol daily price panels into ONE tidy cross-sectional panel sampled on
a monthly decision grid: one row per (month-end decision date, symbol) with the
computed factor columns + a forward return target aligned `horizon_days` ahead.

Both job kinds consume this: the factor study correlates ONE column against
`fwd_ret` (cross-sectional Rank-IC); the model engines train on ALL columns. The
same builder for both = a single definition of every factor (no drift between the
"research" view and the "model" view).

Pure numpy/pandas (no sklearn/torch) so it is importable in any image; it runs in
the `qlib-lab` worker via the lab service.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.adapters.lab.base import FACTORS

# Daily-series windows for the factors (bars). Mirror the Phase-9 signal core where
# they overlap (12-1 momentum = 252 form / 21 skip; 63-day vol).
_MOM_FORM = 252
_MOM_SKIP = 21
_VOL_WIN = 63
_TREND_WIN = 200
_RSI_WIN = 14
_VOL_FAST = 21
_VOL_SLOW = 63
# Longest lookback any factor needs — symbols with fewer bars contribute nothing.
_MIN_BARS = _MOM_FORM + 5


def _wilder_rsi(close: pd.Series, win: int = _RSI_WIN) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    # Wilder smoothing == EWM with alpha = 1/win
    roll_up = up.ewm(alpha=1.0 / win, adjust=False).mean()
    roll_down = down.ewm(alpha=1.0 / win, adjust=False).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _symbol_features(close: pd.Series, volume: pd.Series, horizon_days: int) -> pd.DataFrame:
    """Daily factor columns + forward-return target for one symbol."""
    close = close.astype(float)
    ret1 = close.pct_change()
    feats = pd.DataFrame(index=close.index)
    # 12-1 momentum: cumulative return from t-252 to t-21 (skip the last month).
    feats["momentum"] = close.shift(_MOM_SKIP) / close.shift(_MOM_FORM) - 1.0
    # short-term reversal: NEGATIVE of the last-month return (mean-reversion factor).
    feats["reversal"] = -(close / close.shift(_MOM_SKIP) - 1.0)
    # low-vol anomaly: NEGATIVE trailing daily-return volatility (lower vol = higher score).
    feats["lowvol"] = -(ret1.rolling(_VOL_WIN).std())
    # trend: price relative to its 200d moving average.
    feats["trend"] = close / close.rolling(_TREND_WIN).mean() - 1.0
    # RSI(14) Wilder.
    feats["rsi"] = _wilder_rsi(close)
    # volume surge: fast vs slow average volume.
    if volume is not None and volume.notna().any():
        v = volume.astype(float)
        feats["volume"] = v.rolling(_VOL_FAST).mean() / v.rolling(_VOL_SLOW).mean() - 1.0
    else:
        feats["volume"] = np.nan
    # forward return target (horizon trading days ahead).
    feats["fwd_ret"] = close.shift(-horizon_days) / close - 1.0
    return feats


def build_feature_panel(
    panels: dict[str, pd.DataFrame],
    horizon_days: int,
) -> tuple[pd.DataFrame, list[str]]:
    """panels[symbol] = DataFrame(index=DatetimeIndex, columns ['close','volume'])
    → (tidy cross-sectional panel, feature_cols).

    The panel is sampled on a month-end grid (decouples autocorrelated daily
    observations), features are cross-sectionally z-scored within each decision
    date (so every name is comparable), and any row with a missing feature or an
    unmatured target is dropped.
    """
    feature_cols = list(FACTORS)
    rows: list[pd.DataFrame] = []
    for symbol, df in panels.items():
        if df is None or df.empty or len(df) < _MIN_BARS:
            continue
        df = df.sort_index()
        close = df["close"]
        volume = df["volume"] if "volume" in df.columns else None
        feats = _symbol_features(close, volume, horizon_days)
        # Sample at month end = the LAST trading row of each calendar month. Done
        # version-agnostically (groupby on the month Period + tail(1)) instead of
        # resample("ME"), whose alias differs across pandas versions — the qlib-lab
        # image can carry an older pandas than the main backend (pyqlib pins).
        periods = feats.index.to_period("M")
        monthly = feats.groupby(periods, sort=True).tail(1).copy()  # last row per month
        monthly = monthly.dropna(subset=["fwd_ret"])  # need a matured target
        if monthly.empty:
            continue
        monthly = monthly.reset_index()  # DatetimeIndex → first column (the real date)
        monthly.columns = ["date", *feats.columns]
        monthly["symbol"] = symbol
        rows.append(monthly)

    if not rows:
        return pd.DataFrame(columns=["date", "symbol", *feature_cols, "fwd_ret"]), feature_cols

    panel = pd.concat(rows, ignore_index=True)
    # require every feature present (so the cross-section is complete per name)
    panel = panel.dropna(subset=[*feature_cols, "fwd_ret"])
    if panel.empty:
        return panel, feature_cols

    # cross-sectionally z-score each feature within each decision date
    def _z(g: pd.DataFrame) -> pd.DataFrame:
        for c in feature_cols:
            mu = g[c].mean()
            sd = g[c].std(ddof=0)
            g[c] = (g[c] - mu) / sd if sd and np.isfinite(sd) and sd > 0 else 0.0
        return g

    panel = panel.groupby("date", group_keys=False)[panel.columns.tolist()].apply(_z)
    # keep only dates with enough cross-sectional breadth to rank
    counts = panel.groupby("date")["symbol"].transform("size")
    panel = panel[counts >= 5]
    return panel.sort_values(["date", "symbol"]).reset_index(drop=True), feature_cols
