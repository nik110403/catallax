"""Backtest engine factory — dispatch a `BacktestConfig.engine` to an adapter.

Engine #1 (`vectorized`) is the reimplemented numpy/pandas cross-sectional engine
that ships. `vectorbt` is REGISTERED but not enabled — it proves the seam is a
framework (invariant #2: "a backtesting framework, NOT a single engine") and
documents the drop-in path: implement `BacktestEngineAdapter` over vectorbt's
`Portfolio.from_orders` in its OWN worker image (vectorbt's numba/pandas pins must
not collide with the shared backend image), register it here, done — no route /
worker / widget change.
"""
from __future__ import annotations

from app.adapters.backtest.base import BacktestEngineAdapter
from app.adapters.backtest.vectorized import VectorizedEngine
from app.config import Settings
from app.errors import InvalidRequest


class _VectorbtEngineStub(BacktestEngineAdapter):
    """Documented drop-in slot for vectorbt (engine #2). Not enabled: requires its
    own image (numba/pandas pins) — see skills/backtest-engine.md."""

    name = "vectorbt"

    def run(self, *args, **kwargs):  # type: ignore[override]
        raise InvalidRequest(
            "engine 'vectorbt' is a documented drop-in but not enabled in this build; use 'vectorized'"
        )


_ENGINES: dict[str, type[BacktestEngineAdapter]] = {
    VectorizedEngine.name: VectorizedEngine,
    _VectorbtEngineStub.name: _VectorbtEngineStub,
}


def build_backtest_engine(name: str, settings: Settings) -> BacktestEngineAdapter:
    cls = _ENGINES.get((name or settings.backtest_default_engine).strip().lower())
    if cls is None:
        raise InvalidRequest(f"unknown backtest engine '{name}' (have: {', '.join(sorted(_ENGINES))})")
    return cls()


def known_engines() -> list[str]:
    return sorted(_ENGINES)


# Registered AND runnable. `vectorbt` is registered (to prove the seam) but disabled,
# so pre-flight can reject it cleanly instead of queueing a job doomed to fail.
_DISABLED = {_VectorbtEngineStub.name}


def enabled_engines() -> list[str]:
    return sorted(set(_ENGINES) - _DISABLED)
