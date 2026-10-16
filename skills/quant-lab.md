# Quant / ML Research Lab

Career-grade quant/ML capability — factor research, an ML return-prediction model
zoo, and technical scanners — **isolated** in a heavy opt-in container so it never
bloats the daily stack. Off by default; enable with `docker compose --profile lab
up -d`.

Read this before touching anything under `app/adapters/lab/`, `app/services/lab.py`,
`app/lab_worker.py`, `app/routers/lab.py`, the scanner block in
`app/services/signals.py`, or the Quant Lab frontend widgets.

## Architecture (mirrors the Phase-9 backtester, deliberately)

```
 Browser ── /api/v1/lab/* ──► fastapi-backend (main image, NO heavy ML deps)
                                   │  enqueue + read only
                                   ▼
                         Postgres `lab_runs` table  ◄─── results
                                   ▲
                         Redis `catallax:lab:queue`
                                   │  claim (LMOVE reliable queue)
                                   ▼
        qlib-lab worker  (HEAVY image: torch/xgboost/qlib, --profile lab)
        `python -m app.lab_worker`  ── READ-ONLY F0 daily_bars ──► HistoryStore
```

- **Two job kinds** share `lab_runs` (1 table = job + result, like `backtests`):
  `factor` (cross-sectional IC study) and `model` (return-prediction train/eval).
  `kind` discriminates; `engine` is the factory key.
- **Off-API**: the main API only INSERTs a queued row + SELECTs. The `qlib-lab`
  worker claims it (atomic `UPDATE … WHERE status='queued'`), runs the heavy
  compute in a thread, persists the result, publishes SSE progress. So a slow
  model train can NEVER block the FastAPI event loop — acceptance: "daily stack
  unaffected when the profile is off."
- **Crash recovery / heartbeat / flood-guard / SSE**: identical to the backtester
  (`recover()`, `_heartbeat()`, `_publish()`, `LAB_MAX_QUEUED`). See
  `skills/backtest-engine.md` for the shared rationale.
- **F0 safety**: the worker reads `daily_bars` via `HistoryStore` (idempotent
  upsert) and writes ONLY `lab_runs`. No write path to F0 / signals.

## The image split (why a second Dockerfile)

The backtester reuses the `./backend` image (numpy/pandas already there). Phase 11
needs torch/xgboost, which must NOT enter the daily backend image. So:

- `backend/Dockerfile.lab` = `FROM python:3.12-slim` → CPU-only torch (PyTorch
  wheel index, avoids the multi-GB CUDA build) → `requirements-lab.txt`
  (sklearn/xgboost) → the backend package LAST (a fast pure-python layer, so a lab
  source edit does NOT re-download torch). Heavy layers are deliberately ordered
  first to stay cache-stable across app edits.
- **pyqlib is intentionally NOT installed.** Its hard pins (older pandas/numpy)
  mutated the image for zero runtime benefit — `deep-zoo` builds its LSTM/
  Transformer/MLP with **torch directly**, not qlib's workflow. The full
  qlib-workflow path is deferred. (Defensive corollary: `features.py` samples the
  monthly grid version-agnostically — `groupby(to_period("M")).tail(1)`, not
  `resample("ME")` — so the worker is robust to whatever pandas the image carries.)
- The main backend imports only the **import-light** lab modules
  (`adapters/lab/base.py`, `adapters/lab/factory.py`, `services/lab.py`) — these
  pull numpy/pandas/pydantic only. The factory **lazy-imports** the heavy engine
  classes (`sklearn_ensemble.py`, `deep_zoo.py`) so `known_engines()` works in the
  main image without sklearn/torch present.

## Model-engine seam (the `LabModelEngine` ABC + factory)

Mirrors the Phase-9 `BacktestEngineAdapter` ABC + vectorbt-stub pattern — a
framework, not a single model.

| Engine key | Models | Deps | Status |
|---|---|---|---|
| `sklearn-ensemble` | `xgb` / `rf` / `ensemble` | xgboost + scikit-learn | **Ships, always runs.** The acceptance-satisfying engine. |
| `deep-zoo` | `mlp` / `lstm` / `transformer` | torch | Runs **iff** torch imported in the image; else `available=False` and pre-flight rejects `engine=deep-zoo` with a clean 400. |

`deep-zoo` builds the deep models with **torch directly** (not the full pyqlib
workflow) — far more reliable than fighting qlib's `DatasetH`, while still a real
LSTM/Transformer/MLP zoo. To add an engine: implement
`LabModelEngine.train_eval`, register the name in `factory._ENGINE_NAMES` + a lazy
branch in `build_lab_engine`. No route/worker/widget change.

`train_eval(cfg, panel, feature_cols, progress)` receives a **tidy cross-sectional
panel** (`features.build_feature_panel`): one row per (month-end decision date,
symbol) with z-scored factor columns + a `fwd_ret` target aligned `horizon_days`
ahead. The engine does its OWN **time-ordered** split (`base.time_split` — never
shuffle, leakage) and returns Rank-IC / R² / hit-rate / decile-spread + feature
importances. The single source of truth for the metric is `base.rank_ic` (pure
numpy Spearman).

## Factors (shared by the study AND the model features)

`adapters/lab/features.py` computes six factors per symbol from F0 closes/volume,
sampled monthly, cross-sectionally z-scored: `momentum` (12-1), `reversal`
(short-term, negated), `lowvol` (negated 63d vol), `trend` (price/SMA200),
`rsi` (Wilder-14), `volume` (fast/slow avg-volume surge). The factor study
(`adapters/lab/factors.py`) correlates ONE against `fwd_ret`; the model trains on
ALL. Same builder for both = no drift between the research view and the model view.

## Technical scanners (`services/signals.py`, synchronous)

`ScannerEngine` runs pure-numpy chart-pattern detectors over the F0 **OHLCV**
backbone in the MAIN API (no worker — same cost class as the risk/research
engines, cached): `engulfing`, `hammer_star`, `keltner_break`, `volume_spike`,
`crash`, `moonshot`, `bull_trap`, `bear_trap`. Each carries a directional bias
(±1). `GET /lab/scan?emit=true` POSTs detections as Phase-9 `scan_<pattern>`
signals so they feed the same Rank-IC grading. Add a pattern: extend
`SCAN_PATTERNS` + a branch in `_detect`.

## Endpoints (`/api/v1/lab/*`)

| Method · path | Purpose |
|---|---|
| `POST /lab/factors` | validate → enqueue a factor-IC study (off-API job) |
| `POST /lab/models` | validate → enqueue a model train/eval (off-API job) |
| `GET /lab/runs` | recent runs (list) |
| `GET /lab/runs/{id}` | one run's status (poll target) |
| `GET /lab/runs/{id}/report` | `Envelope[FactorReport \| ModelReport]` (done runs) |
| `GET /lab/runs/{id}/stream` | SSE live progress (Redis pub/sub) |
| `GET /lab/scan` | synchronous technical-pattern scan over F0 |
| `GET /lab/engines` | static catalog (engines, factors, scan patterns) for the UI |

`{id}` is a server-minted UUID, route-alphabet-bounded. Scan `patterns`/`universe`
are comma lists with a bounded charset; symbols never hit an upstream un-validated.

## Frontend (Quant Lab tab, workspaces v10, namespace `quant-lab`)

`FactorResearch.tsx` (id `lab-factor`), `ModelZoo.tsx` (`lab-models`),
`Scanner.tsx` (`lab-scanner`), category "Quant Lab"; the existing
`signal-scoreboard` fills the SignalScores slot. The two runners mirror
`BacktestLab` (config form → POST → `<JobStatus streamUrl={labStreamUrl}>` SSE →
inline report). `JobStatus` gained an optional `streamUrl` prop (defaults to the
backtester) so it is shared.

## Operating it

- Build: `docker compose --profile lab build qlib-lab` (slow first time — torch +
  xgboost downloads; cache-stable after). Start: `docker compose --profile lab up
  -d qlib-lab` (waits for backend healthy → migrations applied). After a backend
  code change, rebuild `fastapi-backend` AND `qlib-lab` (they share `./backend`
  source; the lab image's app layer is last so this is fast).
- All tunables are `LAB_*` env vars (see `.env.example`); no hardcoding.
- A run failing with "engine 'deep-zoo' … torch is not installed" means the torch
  layer didn't land — `sklearn-ensemble` still works (acceptance is satisfied by
  it alone).
