"""Factor research / IC study — pure pandas, the `POST /lab/factors` core.

Given the tidy cross-sectional panel (from `features.build_feature_panel`), study
ONE factor: its cross-sectional Rank-IC against the forward return each period, the
IC summary stats (mean / std / IR / t-stat / hit-rate), the IC time series, and the
quantile-bucket forward returns + long-short spread. This is the classic alpha-
factor evaluation — no model, no torch.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.adapters.lab.base import (
    FactorConfig,
    FactorReport,
    ICPoint,
    ProgressFn,
    QuantileReturn,
    _f,
    _rankdata,
    rank_ic,
)


def run_factor_study(
    cfg: FactorConfig,
    panel: pd.DataFrame,
    progress: ProgressFn | None = None,
) -> FactorReport:
    warnings: list[str] = []
    col = cfg.factor
    if progress:
        progress(0.6, "computing IC series")

    ic_points: list[ICPoint] = []
    ics: list[float] = []
    # per-date quantile bucket accumulation
    q = cfg.quantiles
    bucket_sum = np.zeros(q)
    bucket_cnt = np.zeros(q)
    n_periods = 0
    n_obs = 0

    for d, g in panel.groupby("date", sort=True):
        fv = g[col].to_numpy(dtype=float)
        fwd = g["fwd_ret"].to_numpy(dtype=float)
        ds = pd.Timestamp(d).date().isoformat()
        if fv.size < q:
            ic_points.append(ICPoint(date=ds, ic=None))
            continue
        ic = rank_ic(fv, fwd)
        ic_points.append(ICPoint(date=ds, ic=_f(ic)))
        if ic is not None:
            ics.append(ic)
        n_periods += 1
        n_obs += fv.size
        # bucket by factor rank within this date
        ranks = _rankdata(fv)
        bucket = np.clip(np.ceil(ranks / fv.size * q).astype(int), 1, q)
        for b in range(1, q + 1):
            sel = bucket == b
            if sel.any():
                bucket_sum[b - 1] += float(np.nanmean(fwd[sel]))
                bucket_cnt[b - 1] += 1

    if progress:
        progress(0.85, "aggregating")

    ic_arr = np.array(ics, dtype=float)
    ic_mean = float(ic_arr.mean()) if ic_arr.size else None
    ic_std = float(ic_arr.std(ddof=1)) if ic_arr.size > 1 else None
    ic_ir = (ic_mean / ic_std) if (ic_mean is not None and ic_std not in (None, 0.0)) else None
    ic_tstat = (
        ic_mean / (ic_std / np.sqrt(ic_arr.size))
        if (ic_mean is not None and ic_std not in (None, 0.0) and ic_arr.size > 1)
        else None
    )
    ic_hit_rate = float((ic_arr > 0).mean()) if ic_arr.size else None

    quantile_returns: list[QuantileReturn] = []
    for b in range(q):
        mean_ret = (bucket_sum[b] / bucket_cnt[b]) if bucket_cnt[b] > 0 else None
        quantile_returns.append(QuantileReturn(
            quantile=b + 1,
            mean_fwd_ret=_f(mean_ret),
            n_avg=_f(n_obs / n_periods / q) if n_periods else None,
        ))
    top = quantile_returns[-1].mean_fwd_ret
    bot = quantile_returns[0].mean_fwd_ret
    long_short = (top - bot) if (top is not None and bot is not None) else None

    if n_periods == 0:
        warnings.append("no decision periods had enough cross-sectional breadth for an IC")

    dates = [p.date for p in ic_points]
    return FactorReport(
        name=cfg.name,
        factor=cfg.factor,
        horizon_days=cfg.horizon_days,
        window_start=dates[0] if dates else None,
        window_end=dates[-1] if dates else None,
        n_periods=n_periods,
        n_symbols=int(panel["symbol"].nunique()),
        n_obs=n_obs,
        ic_mean=_f(ic_mean),
        ic_std=_f(ic_std),
        ic_ir=_f(ic_ir),
        ic_tstat=_f(ic_tstat),
        ic_hit_rate=_f(ic_hit_rate),
        ic_series=ic_points,
        quantile_returns=quantile_returns,
        long_short_spread=_f(long_short),
        warnings=warnings,
    )
