"""Ridge regression on ``log(days + 1)`` (time_to_fill; Epic 48 A4) with a residual-quantile P20-P80 range."""

from collections.abc import Sequence

import numpy as np
from sklearn.linear_model import Ridge

HYPER = {"alpha": 1.0, "target": "log1p(days)", "interval": "training residual q20..q80"}


def fit(z: Sequence[Sequence[float]], y: Sequence[float]) -> tuple[float, list[float], float, float]:
    x = np.asarray(z, dtype=np.float64)
    target = np.asarray(y, dtype=np.float64)
    model = Ridge(alpha=1.0)
    model.fit(x, target)
    residuals = target - model.predict(x)
    q20, q80 = np.quantile(residuals, [0.2, 0.8])
    return float(model.intercept_), [float(w) for w in model.coef_], float(q20), float(q80)
