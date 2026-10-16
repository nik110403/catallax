"""Lab model-engine factory — dispatch `ModelConfig.engine` to an adapter.

CRITICAL: the engine modules import the heavy ML stack (sklearn / xgboost / torch),
which is ONLY present in the `qlib-lab` worker image. The main fastapi-backend
imports THIS module (for `known_engines()` in pre-flight / the router) but must NOT
pull those deps — so engine classes are imported LAZILY inside `build_lab_engine`.
`known_engines()` returns the static name registry without importing anything.

`sklearn-ensemble` ships and always runs. `deep-zoo` (torch) runs iff torch landed
in the image — its adapter sets `available=False` otherwise and pre-flight rejects
the job cleanly (the Phase-9 vectorbt-stub pattern).
"""
from __future__ import annotations

from app.adapters.lab.base import LabModelEngine
from app.config import Settings
from app.errors import InvalidRequest

# Static name registry (no heavy import). Keep in sync with the engine modules.
_ENGINE_NAMES = ("sklearn-ensemble", "deep-zoo")


def known_engines() -> list[str]:
    return sorted(_ENGINE_NAMES)


def build_lab_engine(name: str, settings: Settings) -> LabModelEngine:
    """Instantiate an engine by factory key (lazy heavy import). Raises
    InvalidRequest for an unknown key OR a registered-but-unavailable engine."""
    key = (name or settings.lab_default_model_engine).strip().lower()
    if key == "sklearn-ensemble":
        from app.adapters.lab.sklearn_ensemble import SklearnEnsembleEngine
        return SklearnEnsembleEngine()
    if key == "deep-zoo":
        from app.adapters.lab.deep_zoo import DeepZooEngine
        eng = DeepZooEngine()
        if not eng.available:
            raise InvalidRequest(
                "engine 'deep-zoo' is registered but torch is not installed in this "
                "image; use 'sklearn-ensemble'"
            )
        return eng
    raise InvalidRequest(f"unknown lab engine '{name}' (have: {', '.join(known_engines())})")
