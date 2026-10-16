"""Quant/ML Lab contracts — configs, reports, and the model-engine ABC.

Kept IMPORT-LIGHT: numpy + pandas + pydantic only (NO sklearn / xgboost / torch /
qlib). This module is imported by `routers/lab.py` + `services/lab.py`, which run
in the MAIN fastapi-backend image where the heavy ML stack is absent. The heavy
engines (`adapters/lab/sklearn_ensemble.py`, `adapters/lab/deep_zoo.py`) are
imported LAZILY by the factory, only inside the `qlib-lab` worker.

Two job kinds share this contract:
  • factor — cross-sectional IC / quantile study of a computed factor (pure
    pandas; `adapters/lab/factors.py`).
  • model — train + time-ordered-holdout evaluation of a return-prediction model
    via a `LabModelEngine` (`sklearn-ensemble` ships; `deep-zoo` is the optional
    torch LSTM/Transformer/MLP zoo).

The single source of truth for the evaluation metric is the Rank-IC (Spearman) —
the same notion the Phase-9 signal grader / backtester use, reimplemented here in
pure numpy so this layer never imports the service layer.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator

# Charset guard mirrors the signal/backtest core: a symbol becomes part of Redis
# keys + upstream lookups, so bound the alphabet.
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.^=\-]+$")

# Coarse progress sink (fraction 0..1, phase label). Sync — called from the engine
# thread; the worker bridges it back to Redis. Optional / no-op by default.
ProgressFn = Callable[[float, str], None]

# The computed factors available to the factor study + as model features. Each is a
# pure function of a price panel (see adapters/lab/features.py).
FACTORS = ("momentum", "reversal", "lowvol", "trend", "rsi", "volume")
FactorName = Literal["momentum", "reversal", "lowvol", "trend", "rsi", "volume"]


# ── pure numpy Rank-IC (Spearman) — single source of truth for the metric ──
def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average ranks (ties shared), like scipy.stats.rankdata — pure numpy."""
    order = a.argsort(kind="mergesort")
    ranks = np.empty(a.shape[0], dtype=float)
    ranks[order] = np.arange(1, a.shape[0] + 1, dtype=float)
    # average tied ranks
    sa = a[order]
    i = 0
    n = sa.shape[0]
    while i < n:
        j = i + 1
        while j < n and sa[j] == sa[i]:
            j += 1
        if j - i > 1:
            avg = (i + 1 + j) / 2.0  # mean of the 1-based rank block
            ranks[order[i:j]] = avg
        i = j
    return ranks


def rank_ic(pred: np.ndarray, real: np.ndarray) -> float | None:
    """Spearman rank correlation of predictions vs realised forward returns."""
    pred = np.asarray(pred, dtype=float)
    real = np.asarray(real, dtype=float)
    m = np.isfinite(pred) & np.isfinite(real)
    pred, real = pred[m], real[m]
    if pred.size < 3:
        return None
    rp, rr = _rankdata(pred), _rankdata(real)
    rp = rp - rp.mean()
    rr = rr - rr.mean()
    denom = np.sqrt((rp * rp).sum() * (rr * rr).sum())
    if denom <= 0:
        return None
    return float((rp * rr).sum() / denom)


def _f(x: object, nd: int = 6) -> float | None:
    """JSON-safe rounded float (NaN/inf/None -> None)."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return round(v, nd)


def _clean_universe(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    out: list[str] = []
    for s in v:
        s = s.strip().upper()
        if s and not _SYMBOL_RE.match(s):
            raise ValueError(f"universe symbol has invalid characters: {s}")
        if s:
            out.append(s)
    return out or None


# ── configs ────────────────────────────────────────────────
class FactorConfig(BaseModel):
    """A cross-sectional factor IC / quantile study — NO code execution."""

    name: str = Field(min_length=1, max_length=128)
    factor: FactorName = "momentum"
    universe: list[str] | None = Field(default=None, max_length=2000)
    start: date | None = None
    end: date | None = None
    horizon_days: int = Field(default=21, ge=1, le=252)
    quantiles: int = Field(default=5, ge=2, le=10)

    @field_validator("universe")
    @classmethod
    def _ck_universe(cls, v: list[str] | None) -> list[str] | None:
        return _clean_universe(v)


class ModelConfig(BaseModel):
    """Train + evaluate a return-prediction model — declarative, NO code execution.

    `engine` selects the factory adapter; `model` selects a variant within it
    (sklearn-ensemble: xgb | rf | ensemble; deep-zoo: mlp | lstm | transformer)."""

    name: str = Field(min_length=1, max_length=128)
    engine: str = Field(default="sklearn-ensemble", max_length=32)
    model: str = Field(default="ensemble", max_length=24)
    universe: list[str] | None = Field(default=None, max_length=2000)
    start: date | None = None
    end: date | None = None
    horizon_days: int = Field(default=21, ge=1, le=252)
    features: list[str] | None = Field(default=None, max_length=32)
    test_fraction: float = Field(default=0.3, ge=0.1, le=0.5)

    @field_validator("universe")
    @classmethod
    def _ck_universe(cls, v: list[str] | None) -> list[str] | None:
        return _clean_universe(v)

    @field_validator("engine")
    @classmethod
    def _ck_engine(cls, v: str) -> str:
        # Charset guard parity with `model` below: the raw engine string is echoed in
        # a 400 message + stored/reflected in LabJob, so bound the alphabet at the edge
        # (the known_engines() allowlist is the functional gate; this is defence-in-depth).
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9_\-]+$", v):
            raise ValueError("engine has invalid characters")
        return v

    @field_validator("model")
    @classmethod
    def _ck_model(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9_\-]+$", v):
            raise ValueError("model has invalid characters")
        return v

    @field_validator("features")
    @classmethod
    def _ck_features(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out = [f.strip().lower() for f in v if f.strip()]
        bad = [f for f in out if f not in FACTORS]
        if bad:
            raise ValueError(f"unknown feature(s): {', '.join(bad)} (have: {', '.join(FACTORS)})")
        return out or None


# ── report models ──────────────────────────────────────────
class ICPoint(BaseModel):
    date: str
    ic: float | None


class QuantileReturn(BaseModel):
    quantile: int  # 1 = lowest factor/prediction bucket … Q = highest
    mean_fwd_ret: float | None  # mean forward return of names in this bucket (fraction)
    n_avg: float | None  # average names per period in this bucket


class FeatureImportance(BaseModel):
    feature: str
    importance: float | None


class FactorReport(BaseModel):
    name: str
    factor: str
    horizon_days: int
    window_start: str | None
    window_end: str | None
    n_periods: int
    n_symbols: int
    n_obs: int
    ic_mean: float | None
    ic_std: float | None
    ic_ir: float | None  # IC mean / IC std (annualised information ratio of the IC series)
    ic_tstat: float | None
    ic_hit_rate: float | None  # fraction of periods with IC > 0
    ic_series: list[ICPoint]
    quantile_returns: list[QuantileReturn]
    long_short_spread: float | None  # top minus bottom bucket mean forward return
    warnings: list[str]


class ModelMetrics(BaseModel):
    test_ic: float | None  # Spearman rank-IC of test predictions vs realised
    test_r2: float | None
    hit_rate: float | None  # fraction of test names where sign(pred) == sign(real)
    long_short_decile_spread: float | None  # top-decile minus bottom-decile realised return
    n_train: int
    n_test: int
    n_features: int


class ModelReport(BaseModel):
    name: str
    engine: str
    model: str
    horizon_days: int
    window_start: str | None
    window_end: str | None
    metrics: ModelMetrics
    feature_importance: list[FeatureImportance]
    pred_quantile_returns: list[QuantileReturn]  # realised returns by predicted decile/bucket
    warnings: list[str]


# ── model-engine ABC ───────────────────────────────────────
class LabModelEngine(ABC):
    """Train + evaluate a return-prediction model on a feature panel.

    `panel` is a tidy DataFrame with columns: ['date', 'symbol', <feature cols…>,
    'fwd_ret'] — one row per (decision date, symbol), the target already aligned to
    `cfg.horizon_days` ahead. The engine does the TIME-ORDERED split itself (never
    shuffle — leakage), fits, and returns a `ModelReport`. Implementations live in
    the heavy `qlib-lab` image only.
    """

    name: str = "base"
    available: bool = True  # engines flip this False if their heavy import is missing

    @abstractmethod
    def train_eval(
        self,
        cfg: ModelConfig,
        panel: pd.DataFrame,
        feature_cols: list[str],
        progress: ProgressFn | None = None,
    ) -> ModelReport:
        ...


def time_split(panel: pd.DataFrame, test_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a tidy panel into train/test by a date cutoff (no leakage: the test
    set is strictly the most-recent `test_fraction` of distinct decision dates)."""
    dates = np.sort(panel["date"].unique())
    if dates.size < 2:
        return panel.iloc[:0], panel.iloc[:0]
    cut_idx = max(1, int(round(dates.size * (1.0 - test_fraction))))
    cut_idx = min(cut_idx, dates.size - 1)  # guarantee ≥1 test date
    cutoff = dates[cut_idx]
    train = panel[panel["date"] < cutoff]
    test = panel[panel["date"] >= cutoff]
    return train, test


def quantile_returns(values: np.ndarray, fwd: np.ndarray, q: int) -> list[QuantileReturn]:
    """Bucket `values` into q quantiles, report mean forward return per bucket."""
    out: list[QuantileReturn] = []
    m = np.isfinite(values) & np.isfinite(fwd)
    values, fwd = values[m], fwd[m]
    if values.size < q:
        return out
    ranks = _rankdata(values)
    edges = np.linspace(0, values.size, q + 1)
    bucket = np.clip(np.ceil(ranks / values.size * q).astype(int), 1, q)
    for b in range(1, q + 1):
        sel = bucket == b
        out.append(QuantileReturn(
            quantile=b,
            mean_fwd_ret=_f(fwd[sel].mean()) if sel.any() else None,
            n_avg=_f(float(sel.sum())),
        ))
    return out
