# ruff: noqa: E501
"""Classical, explainable models (Epic 48 A4/A5). **The only package that imports numpy / scikit-learn**
(``tests/modules/analytics/test_import_guard.py``).

* ``fit(task, spec_features, train, holdout, seed)`` -> ``FitResult(params, metrics, baseline, beats_baseline)``.
  ``params`` holds only decimal strings (``serialize.dec``) and is byte-reproducible from the same rows + seed.
* ``predict(task, params, features)`` (pure Python) -> ``Prediction``: the linear term, per-feature contributions
  (coefficient x standardised value, summed over a onehot feature's columns), and the probability (logistic) or
  the predicted days with its P20-P80 range (ridge).
"""

import dataclasses
import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.services.analytics.models import scaler
from app.services.analytics.models.serialize import dec, num

Task = Literal["classification", "regression"]
LabelledRow = tuple[Mapping[str, Any], float]


@dataclasses.dataclass(frozen=True, slots=True)
class FitResult:
    params: dict[str, Any]
    metrics: dict[str, Any]
    baseline: dict[str, Any]
    beats_baseline: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Prediction:
    linear: float
    intercept: float
    contributions: dict[str, float]  # feature key -> contribution (log-odds or log-days)
    probability: float | None = None
    days: float | None = None
    p20: float | None = None
    p80: float | None = None


def _matrix(rows: Sequence[LabelledRow], encoder: Mapping[str, Any]) -> list[list[float]]:
    return [scaler.standardize(scaler.encode(f, encoder), encoder) for f, _ in rows]


def fit(
    task: Task,
    spec_features: Sequence[Mapping[str, Any]],
    train: Sequence[LabelledRow],
    holdout: Sequence[LabelledRow],
    seed: int,
) -> FitResult:
    from app.services.analytics.models import logistic, metrics, ridge

    encoder = scaler.build_encoder(spec_features, [f for f, _ in train])
    z_train = _matrix(train, encoder)
    z_hold = _matrix(holdout, encoder)
    cols = encoder["columns"]
    if task == "classification":
        y = [float(lab) for _, lab in train]
        intercept, weights = logistic.fit(z_train, y, seed)
        params: dict[str, Any] = {
            "task": task,
            "intercept": dec(intercept),
            "coefficients": {c: dec(w) for c, w in zip(cols, weights, strict=True)},
            "encoder": encoder,
            "hyper": {k: str(v) for k, v in logistic.HYPER.items()},
            "seed": seed,
        }
        p = [
            logistic.sigmoid(intercept + sum(w * x for w, x in zip(weights, row, strict=True)))
            for row in z_hold
        ]
        base_rate = sum(y) / len(y)
        m, base, beats = metrics.classification(p, [lab for _, lab in holdout], base_rate)
        return FitResult(params, m, base, beats)
    days = [float(lab) for _, lab in train]
    target = [math.log1p(max(d, 0.0)) for d in days]
    intercept, weights, q20, q80 = ridge.fit(z_train, target)
    params = {
        "task": task,
        "intercept": dec(intercept),
        "coefficients": {c: dec(w) for c, w in zip(cols, weights, strict=True)},
        "encoder": encoder,
        "residual_q20": dec(q20),
        "residual_q80": dec(q80),
        "hyper": {k: str(v) for k, v in ridge.HYPER.items()},
        "seed": seed,
    }
    pred = [math.expm1(intercept + sum(w * x for w, x in zip(weights, row, strict=True))) for row in z_hold]
    m, base, beats = metrics.regression(pred, [lab for _, lab in holdout], metrics.median(days))
    return FitResult(params, m, base, beats)


def predict(params: Mapping[str, Any], features: Mapping[str, Any]) -> Prediction:
    """Pure Python (no numpy): safe in request paths and identical to the fitted model's arithmetic."""
    encoder = params["encoder"]
    z = scaler.standardize(scaler.encode(features, encoder), encoder)
    intercept = num(params["intercept"])
    contributions: dict[str, float] = {}
    for col, value in zip(encoder["columns"], z, strict=True):
        key = encoder["column_feature"][col]
        contributions[key] = contributions.get(key, 0.0) + num(params["coefficients"][col]) * value
    linear = intercept + sum(contributions.values())
    if params["task"] == "classification":
        from app.services.analytics.models.logistic import sigmoid

        return Prediction(linear, intercept, contributions, probability=sigmoid(linear))
    q20, q80 = num(params["residual_q20"]), num(params["residual_q80"])
    return Prediction(
        linear,
        intercept,
        contributions,
        days=math.expm1(linear),
        p20=math.expm1(linear + q20),
        p80=math.expm1(linear + q80),
    )


def coefficient_ranks(params: Mapping[str, Any]) -> list[tuple[str, float]]:
    """(feature key, summed |coefficient|) sorted descending -- the model card's top features."""
    encoder = params.get("encoder") or {}
    agg: dict[str, float] = {}
    signed: dict[str, float] = {}
    for col, w in (params.get("coefficients") or {}).items():
        key = encoder.get("column_feature", {}).get(col, col)
        agg[key] = agg.get(key, 0.0) + abs(num(w))
        signed[key] = signed.get(key, 0.0) + num(w)
    return sorted(((k, signed[k]) for k in agg), key=lambda kv: (-agg[kv[0]], kv[0]))


__all__ = ["FitResult", "Prediction", "coefficient_ranks", "fit", "predict"]
