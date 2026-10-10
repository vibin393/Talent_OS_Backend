"""L2 logistic regression (offer_drop, attrition; Epic 48 A4). Fixed hyperparameters, no grid search."""

import math
from collections.abc import Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression

HYPER = {
    "penalty": "l2",
    "l1_ratio": 0.0,
    "C": 1.0,
    "solver": "lbfgs",
    "max_iter": 1000,
    "class_weight": "balanced",
}


def fit(z: Sequence[Sequence[float]], y: Sequence[float], seed: int) -> tuple[float, list[float]]:
    # scikit-learn >= 1.8: L2 is ``l1_ratio=0`` (``penalty='l2'`` is deprecated); same objective.
    model = LogisticRegression(
        l1_ratio=0.0, C=1.0, solver="lbfgs", max_iter=1000, class_weight="balanced", random_state=seed
    )
    model.fit(np.asarray(z, dtype=np.float64), np.asarray(y, dtype=np.int64))
    return float(model.intercept_[0]), [float(w) for w in model.coef_[0]]


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)
