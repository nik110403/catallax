"""`sklearn-ensemble` model engine — XGBoost + RandomForest.

The shipped, reliable return-prediction engine: gradient-boosted trees (XGBoost)
and a random forest, optionally averaged. Trains on the cross-sectional feature
panel with a TIME-ORDERED holdout (no shuffle — leakage), reports Rank-IC / R² /
hit-rate / decile long-short spread + averaged feature importances.

Heavy imports (sklearn / xgboost) are module-level — this file is imported ONLY by
the factory inside the `qlib-lab` worker image, never by the main backend.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.adapters.lab.base import (
    FeatureImportance,
    LabModelEngine,
    ModelConfig,
    ModelMetrics,
    ModelReport,
    ProgressFn,
    QuantileReturn,
    _f,
    quantile_returns,
    rank_ic,
    time_split,
)

logger = logging.getLogger("catallax.lab.sklearn")

_VARIANTS = ("xgb", "rf", "ensemble")


class SklearnEnsembleEngine(LabModelEngine):
    name = "sklearn-ensemble"
    available = True

    def train_eval(
        self,
        cfg: ModelConfig,
        panel: pd.DataFrame,
        feature_cols: list[str],
        progress: ProgressFn | None = None,
    ) -> ModelReport:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.metrics import r2_score
        from xgboost import XGBRegressor

        warnings: list[str] = []
        variant = cfg.model if cfg.model in _VARIANTS else "ensemble"
        if cfg.model not in _VARIANTS:
            warnings.append(f"unknown model '{cfg.model}' for sklearn-ensemble; used 'ensemble'")
        feats = cfg.features or feature_cols

        if progress:
            progress(0.55, "splitting train/test")
        train, test = time_split(panel, cfg.test_fraction)
        if train.empty or test.empty:
            raise ValueError("not enough decision dates to form a train/test split")

        Xtr, ytr = train[feats].to_numpy(float), train["fwd_ret"].to_numpy(float)
        Xte, yte = test[feats].to_numpy(float), test["fwd_ret"].to_numpy(float)

        models = []
        if variant in ("xgb", "ensemble"):
            models.append(("xgb", XGBRegressor(
                n_estimators=300, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, n_jobs=2,
                objective="reg:squarederror", random_state=7,
            )))
        if variant in ("rf", "ensemble"):
            models.append(("rf", RandomForestRegressor(
                n_estimators=300, max_depth=6, min_samples_leaf=20,
                n_jobs=2, random_state=7,
            )))

        if progress:
            progress(0.6, f"fitting {variant}")
        preds = np.zeros(Xte.shape[0])
        importances = np.zeros(len(feats))
        for i, (tag, m) in enumerate(models):
            m.fit(Xtr, ytr)
            preds += m.predict(Xte)
            imp = getattr(m, "feature_importances_", None)
            if imp is not None:
                importances += np.asarray(imp, dtype=float)
            if progress:
                progress(0.6 + 0.25 * (i + 1) / len(models), f"fitted {tag}")
        preds /= len(models)
        importances /= len(models)

        if progress:
            progress(0.9, "evaluating")

        # mean per-date Rank-IC on the test set (consistent with the factor study)
        test = test.assign(_pred=preds)
        per_date_ic = []
        for _, g in test.groupby("date"):
            ic = rank_ic(g["_pred"].to_numpy(float), g["fwd_ret"].to_numpy(float))
            if ic is not None:
                per_date_ic.append(ic)
        test_ic = float(np.mean(per_date_ic)) if per_date_ic else rank_ic(preds, yte)

        try:
            r2 = float(r2_score(yte, preds))
        except (ValueError, TypeError):  # zero-variance / NaN arrays — the degenerate cases sklearn raises
            r2 = None
        hit = float((np.sign(preds) == np.sign(yte)).mean()) if yte.size else None

        pqr: list[QuantileReturn] = quantile_returns(preds, yte, 5)
        spread = None
        if len(pqr) == 5 and pqr[-1].mean_fwd_ret is not None and pqr[0].mean_fwd_ret is not None:
            spread = pqr[-1].mean_fwd_ret - pqr[0].mean_fwd_ret

        fi = sorted(
            (FeatureImportance(feature=f, importance=_f(importances[i])) for i, f in enumerate(feats)),
            key=lambda x: x.importance or 0.0, reverse=True,
        )

        dates = np.sort(panel["date"].unique())
        return ModelReport(
            name=cfg.name,
            engine=self.name,
            model=variant,
            horizon_days=cfg.horizon_days,
            window_start=pd.Timestamp(dates[0]).date().isoformat() if dates.size else None,
            window_end=pd.Timestamp(dates[-1]).date().isoformat() if dates.size else None,
            metrics=ModelMetrics(
                test_ic=_f(test_ic),
                test_r2=_f(r2),
                hit_rate=_f(hit),
                long_short_decile_spread=_f(spread),
                n_train=int(Xtr.shape[0]),
                n_test=int(Xte.shape[0]),
                n_features=len(feats),
            ),
            feature_importance=fi,
            pred_quantile_returns=pqr,
            warnings=warnings,
        )
