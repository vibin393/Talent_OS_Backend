# ruff: file-ignore[line-too-long]
"""Holdout metrics against the naive baseline (Epic 48 A4).

* Classification: AUC, Brier score and a 10-bin calibration table; the baseline predicts the training base rate
  (AUC 0.5, ``brier_base``). Beats baseline <=> ``AUC >= 0.55`` and ``Brier < brier_base``.
* Regression: MAE in days; the baseline predicts the training median of days. Beats baseline <=> ``MAE < mae_base``.
"""

from collections.abc import Sequence
from typing import Any

import numpy as np
from sklearn.metrics import roc_auc_score

from app.services.analytics.models.serialize import dec

MIN_AUC = 0.55


def classification(
    p: Sequence[float], y: Sequence[float], base_rate: float
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    pa = np.asarray(p, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    auc = float(roc_auc_score(ya, pa)) if 0 < ya.sum() < len(ya) else None
    brier = float(np.mean((pa - ya) ** 2))
    brier_base = float(np.mean((base_rate - ya) ** 2))
    calibration = []
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        mask = (pa >= lo) & ((pa < hi) if i < 9 else (pa <= hi))
        n = int(mask.sum())
        if n:
            calibration.append(
                {
                    "bin": i,
                    "predicted": dec(float(pa[mask].mean())),
                    "observed": dec(float(ya[mask].mean())),
                    "n": n,
                }
            )
    beats = auc is not None and auc >= MIN_AUC and brier < brier_base
    metrics = {
        "auc": dec(auc) if auc is not None else None,
        "brier": dec(brier),
        "calibration": calibration,
        "n_holdout": len(ya),
    }
    baseline = {"auc": "0.5", "brier": dec(brier_base), "base_rate": dec(base_rate)}
    return metrics, baseline, beats


def regression(
    pred_days: Sequence[float], days: Sequence[float], train_median: float
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    pa = np.asarray(pred_days, dtype=np.float64)
    da = np.asarray(days, dtype=np.float64)
    mae = float(np.mean(np.abs(pa - da)))
    mae_base = float(np.mean(np.abs(train_median - da)))
    return (
        {"mae_days": dec(mae), "n_holdout": len(da)},
        {"mae_days": dec(mae_base), "median_days": dec(train_median)},
        mae < mae_base,
    )


def median(values: Sequence[float]) -> float:
    return float(np.median(np.asarray(values, dtype=np.float64)))
