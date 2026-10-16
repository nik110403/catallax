"""Backtester engine seam (Phase 9, engine).

A backtesting FRAMEWORK, not a single engine: `BacktestEngineAdapter` (an ABC
mirroring `PriceSourceAdapter`) + a `factory`, with engine #1 a reimplemented
numpy/pandas cross-sectional signal-driven quantile rebalance (`vectorized`).
vectorbt is registered as a documented drop-in (engine #2) to prove the seam.

The engine is PURE compute: it receives already-resolved price panels + signal
rows and returns a `BacktestReport`. All I/O (the F0 fan-out, the Redis/Postgres
job lifecycle) lives in `services/backtest.py` + `app/worker.py`, so the engine
holds no DB/Redis handle and the worker is structurally read-only on F0.
"""
