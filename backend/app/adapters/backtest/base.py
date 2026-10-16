"""Backtester engine contract — the config, the result, and the ABC.

Kept PURE: no DB/Redis/service imports. The engine receives clean numpy panels +
signal rows and returns a `BacktestReport`. Cross-cutting helpers it would reuse
from the signal grader (e.g. the Rank-IC) are INJECTED as callables so this
adapter layer never imports the service layer (clean layering + a single source
of truth for the IC math).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, field_validator, model_validator

# Charset guards mirror the signal core (routers/signals.py): a signal_id / symbol
# becomes part of Redis keys + upstream symbol lookups, so bound the alphabet.
_SIGNAL_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.^=\-]+$")

# A resolved daily-bar panel for one symbol: (ascending dates, close prices).
Panel = tuple[list[date], np.ndarray]
# Reuse the grader's Rank-IC (scores, forward_returns) -> coefficient | None.
RankICFn = Callable[[np.ndarray, np.ndarray], float | None]
# Coarse progress sink (fraction 0..1, phase label). Sync — called from the engine
# thread; the worker bridges it back to Redis. Optional / no-op by default.
ProgressFn = Callable[[float, str], None]


class BacktestConfig(BaseModel):
    """Declarative, qlib-shaped strategy spec — NO code execution. The strategy IS
    a logged `signal_id` (the join surface to deliverable #1): rank its scores
    cross-sectionally each rebalance, hold the top quantile to the next rebalance."""

    name: str = Field(min_length=1, max_length=128)
    engine: str = Field(default="vectorized", max_length=32)
    signal_id: str = Field(max_length=64)
    # None = every symbol logged for this signal_id; else an explicit allowlist.
    # max_length fails oversized lists at the parse boundary (defence-in-depth; the
    # business cap is BACKTEST_MAX_SYMBOLS, enforced in pre-flight validate()).
    universe: list[str] | None = Field(default=None, max_length=2000)
    start: date
    end: date
    rebalance: Literal["W", "M", "Q"] = "M"
    # Long the top 1/quantile bucket (5 = quintiles). Bottom bucket shorted iff long_short.
    quantile: int = Field(default=5, ge=2, le=10)
    long_short: bool = False
    weighting: Literal["equal", "score"] = "equal"
    costs_bps: float = Field(default=10.0, ge=0.0, le=1000.0)
    benchmark_symbol: str | None = Field(default=None, max_length=32)
    rf_annual: float = Field(default=0.0, ge=-1.0, le=1.0)

    @field_validator("signal_id")
    @classmethod
    def _ck_signal_id(cls, v: str) -> str:
        if not _SIGNAL_ID_RE.match(v):
            raise ValueError("signal_id has invalid characters")
        return v

    @field_validator("universe")
    @classmethod
    def _ck_universe(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out = []
        for s in v:
            s = s.strip().upper()
            if s and not _SYMBOL_RE.match(s):
                raise ValueError(f"universe symbol has invalid characters: {s}")
            if s:
                out.append(s)
        return out or None

    @field_validator("benchmark_symbol")
    @classmethod
    def _ck_bench(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not v:
            return None
        if not _SYMBOL_RE.match(v):
            raise ValueError("benchmark_symbol has invalid characters")
        return v

    @model_validator(mode="after")
    def _ck_window(self) -> "BacktestConfig":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


# ── result models ──────────────────────────────────────────────
class EquityPoint(BaseModel):
    date: str
    value: float  # equity index, base 1.0


class DrawdownPoint(BaseModel):
    date: str
    value: float  # drawdown fraction (<= 0)


class RebalancePoint(BaseModel):
    date: str  # decision date T
    entry_date: str | None  # first bar strictly after T (the lookahead-clean entry)
    n_long: int
    n_short: int
    turnover: float  # one-way
    ic: float | None  # cross-sectional Rank-IC of score vs realised next-period return
    status: str  # matured | pending (unsettled tail — excluded from the curve)


class BacktestMetrics(BaseModel):
    cum_return_pct: float | None
    ann_return_pct: float | None
    ann_vol_pct: float | None
    sharpe: float | None
    info_ratio: float | None
    max_drawdown_pct: float | None
    turnover_annualised: float | None  # one-way
    mean_ic: float | None
    n_rebalances: int
    n_matured_periods: int
    n_pending_periods: int
    n_names_avg: float | None
    n_days: int
    benchmark: str  # the IR benchmark description (symbol or "synthetic equal-weight")


class BacktestReport(BaseModel):
    signal_id: str
    engine: str
    name: str
    window_start: str | None
    window_end: str | None
    equity: list[EquityPoint]
    drawdown: list[DrawdownPoint]
    benchmark_equity: list[EquityPoint] | None
    rebalances: list[RebalancePoint]
    metrics: BacktestMetrics
    warnings: list[str]


class BacktestEngineAdapter(ABC):
    """One backtest engine. `run` is pure compute (no I/O) so it can execute in a
    worker thread off the API event loop."""

    name: str = "base"

    @abstractmethod
    def run(
        self,
        config: BacktestConfig,
        panels: dict[str, Panel],
        signal_rows: list[tuple[date, str, float]],
        benchmark_panel: Panel | None,
        settle_days: int,
        rank_ic: RankICFn,
        progress: ProgressFn | None = None,
    ) -> BacktestReport:
        """Run the strategy. `panels[symbol] = (dates, closes)`; `signal_rows` are
        (as_of_date, symbol, score) for `config.signal_id`. Entry MUST be the first
        bar STRICTLY after each rebalance date; the unsettled tail MUST be excluded."""
        raise NotImplementedError
