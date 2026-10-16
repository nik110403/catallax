"""`deep-zoo` model engine — torch MLP / LSTM / Transformer.

The deep return-prediction zoo. `mlp` regresses on the cross-sectional feature
vector; `lstm` / `transformer` build short per-symbol sequences of the monthly
feature vectors (a genuine temporal model) and predict the next forward return.
Trains CPU-only with a time-ordered holdout; reports the same Rank-IC / R² /
hit-rate / decile-spread metrics as the sklearn engine, plus permutation feature
importances.

torch is imported LAZILY (inside the methods). If the import fails — the
`qlib-lab` image's best-effort heavy layer didn't land — the factory marks this
engine `available = False`, the API rejects `engine=deep-zoo` with a clean 400,
and the shipped `sklearn-ensemble` still satisfies the phase. This mirrors the
Phase-9 vectorbt drop-in-stub philosophy.
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
)

logger = logging.getLogger("catallax.lab.deep")

_VARIANTS = ("mlp", "lstm", "transformer")
_SEQ_LEN = {"mlp": 1, "lstm": 6, "transformer": 6}
_EPOCHS = 60
_HIDDEN = 32


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _build_sequences(panel: pd.DataFrame, feats: list[str], seq_len: int):
    """Per-symbol sliding windows → (dates, X[N, L, F], y[N]). date = window end."""
    dates: list[pd.Timestamp] = []
    xs: list[np.ndarray] = []
    ys: list[float] = []
    for _, g in panel.groupby("symbol", sort=False):
        g = g.sort_values("date")
        fm = g[feats].to_numpy(float)
        fwd = g["fwd_ret"].to_numpy(float)
        dd = g["date"].to_numpy()
        for i in range(seq_len - 1, len(g)):
            xs.append(fm[i - seq_len + 1: i + 1])
            ys.append(fwd[i])
            dates.append(pd.Timestamp(dd[i]))
    if not xs:
        return np.array([]), np.zeros((0, seq_len, len(feats))), np.zeros(0)
    return np.array(dates), np.stack(xs), np.asarray(ys, dtype=float)


class DeepZooEngine(LabModelEngine):
    name = "deep-zoo"

    def __init__(self) -> None:
        self.available = _torch_available()

    def train_eval(
        self,
        cfg: ModelConfig,
        panel: pd.DataFrame,
        feature_cols: list[str],
        progress: ProgressFn | None = None,
    ) -> ModelReport:
        import torch
        from torch import nn

        torch.manual_seed(7)
        torch.set_num_threads(max(1, (torch.get_num_threads() or 2)))

        warnings: list[str] = []
        variant = cfg.model if cfg.model in _VARIANTS else "mlp"
        if cfg.model not in _VARIANTS:
            warnings.append(f"unknown model '{cfg.model}' for deep-zoo; used 'mlp'")
        feats = cfg.features or feature_cols
        seq_len = _SEQ_LEN[variant]
        F = len(feats)

        if progress:
            progress(0.5, "building sequences")
        dates, X, y = _build_sequences(panel, feats, seq_len)
        if X.shape[0] < 40:
            raise ValueError("not enough sequence samples to train a deep model")

        # time-ordered split by window-end date
        uniq = np.sort(np.unique(dates))
        cut = uniq[min(max(1, int(round(uniq.size * (1 - cfg.test_fraction)))), uniq.size - 1)]
        tr = dates < cut
        te = ~tr
        if tr.sum() < 20 or te.sum() < 5:
            raise ValueError("train/test split too small for a deep model")

        Xtr = torch.tensor(X[tr], dtype=torch.float32)
        ytr = torch.tensor(y[tr], dtype=torch.float32).unsqueeze(1)
        Xte = torch.tensor(X[te], dtype=torch.float32)
        yte = y[te]

        class MLP(nn.Module):
            def __init__(self):
                super().__init__()
                self.net = nn.Sequential(nn.Linear(F, _HIDDEN), nn.ReLU(), nn.Linear(_HIDDEN, 1))

            def forward(self, x):  # x: [B, 1, F]
                return self.net(x[:, -1, :])

        class LSTM(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(F, _HIDDEN, batch_first=True)
                self.head = nn.Linear(_HIDDEN, 1)

            def forward(self, x):
                out, _ = self.lstm(x)
                return self.head(out[:, -1, :])

        class Transformer(nn.Module):
            def __init__(self):
                super().__init__()
                self.proj = nn.Linear(F, _HIDDEN)
                layer = nn.TransformerEncoderLayer(
                    d_model=_HIDDEN, nhead=4, dim_feedforward=64,
                    batch_first=True, dropout=0.0,
                )
                self.enc = nn.TransformerEncoder(layer, num_layers=1)
                self.head = nn.Linear(_HIDDEN, 1)

            def forward(self, x):
                h = self.enc(self.proj(x))
                return self.head(h.mean(dim=1))

        model = {"mlp": MLP, "lstm": LSTM, "transformer": Transformer}[variant]()
        opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
        loss_fn = nn.MSELoss()

        if progress:
            progress(0.55, f"training {variant}")
        model.train()
        for epoch in range(_EPOCHS):
            opt.zero_grad()
            loss = loss_fn(model(Xtr), ytr)
            loss.backward()
            opt.step()
            if progress and epoch % 15 == 0:
                progress(0.55 + 0.3 * epoch / _EPOCHS, f"epoch {epoch}")

        model.eval()
        with torch.no_grad():
            preds = model(Xte).squeeze(1).numpy()

        if progress:
            progress(0.9, "evaluating")

        te_dates = dates[te]
        per_date_ic = []
        df_eval = pd.DataFrame({"date": te_dates, "pred": preds, "real": yte})
        for _, g in df_eval.groupby("date"):
            ic = rank_ic(g["pred"].to_numpy(float), g["real"].to_numpy(float))
            if ic is not None:
                per_date_ic.append(ic)
        test_ic = float(np.mean(per_date_ic)) if per_date_ic else rank_ic(preds, yte)

        ss_res = float(((yte - preds) ** 2).sum())
        ss_tot = float(((yte - yte.mean()) ** 2).sum())
        r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else None
        hit = float((np.sign(preds) == np.sign(yte)).mean()) if yte.size else None

        pqr: list[QuantileReturn] = quantile_returns(preds, yte, 5)
        spread = None
        if len(pqr) == 5 and pqr[-1].mean_fwd_ret is not None and pqr[0].mean_fwd_ret is not None:
            spread = pqr[-1].mean_fwd_ret - pqr[0].mean_fwd_ret

        # permutation importance: IC drop when each feature column is shuffled
        fi: list[FeatureImportance] = []
        base_ic = test_ic or 0.0
        rng = np.random.default_rng(7)
        Xte_np = X[te].copy()
        for fi_idx, fname in enumerate(feats):
            Xp = Xte_np.copy()
            perm = rng.permutation(Xp.shape[0])
            Xp[:, :, fi_idx] = Xp[perm, :, fi_idx]
            with torch.no_grad():
                pp = model(torch.tensor(Xp, dtype=torch.float32)).squeeze(1).numpy()
            ic_p = rank_ic(pp, yte) or 0.0
            fi.append(FeatureImportance(feature=fname, importance=_f(base_ic - ic_p)))
        fi.sort(key=lambda x: x.importance or 0.0, reverse=True)

        return ModelReport(
            name=cfg.name,
            engine=self.name,
            model=variant,
            horizon_days=cfg.horizon_days,
            window_start=pd.Timestamp(uniq[0]).date().isoformat(),
            window_end=pd.Timestamp(uniq[-1]).date().isoformat(),
            metrics=ModelMetrics(
                test_ic=_f(test_ic),
                test_r2=_f(r2),
                hit_rate=_f(hit),
                long_short_decile_spread=_f(spread),
                n_train=int(tr.sum()),
                n_test=int(te.sum()),
                n_features=F,
            ),
            feature_importance=fi,
            pred_quantile_returns=pqr,
            warnings=warnings,
        )
