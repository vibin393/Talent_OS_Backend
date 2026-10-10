"""Feature encoding and standardisation (Epic 48 A4).

* ``build_encoder(spec_features, train_rows)`` learns, from the **training part only**: onehot categories
  (``categories`` from the spec, else the top ``max_categories`` by frequency, plus ``other``), the imputation
  value of every numeric column (training mean after transform) and the per-column mean / standard deviation.
* ``encode(features, encoder)`` -> the transformed, imputed raw column values; ``standardize`` -> z-scores.
  Both are pure Python so scoring needs no numpy; fitting uses the same functions.

The encoder is part of the model ``params`` (decimal strings): ``{columns, column_feature, impute, means,
scales, categories}``.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.analytics.models.serialize import dec, num

OTHER = "other"


def _transform(value: float, feature: Mapping[str, Any]) -> float:
    kind = feature.get("transform", "none")
    if kind == "log1p":
        return math.log1p(max(value, 0.0))
    if kind == "clip" and feature.get("clip"):
        lo, hi = (float(v) for v in feature["clip"])
        return min(max(value, lo), hi)
    return value


def _category(value: Any, categories: Sequence[str]) -> str:
    if value is None:
        return OTHER
    text = str(value).strip().lower()
    return text if text in categories else OTHER


def build_encoder(
    spec_features: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    columns: list[str] = []
    column_feature: dict[str, str] = {}
    categories: dict[str, list[str]] = {}
    impute: dict[str, str] = {}
    for f in spec_features:
        key = f["key"]
        if f["type"] == "onehot":
            if f.get("categories"):
                cats = [str(c).lower() for c in f["categories"]]
            else:
                counts = Counter(
                    str(r.get(key)).strip().lower() for r in rows if r.get(key) not in (None, "")
                )
                limit = int(f.get("max_categories") or 20)
                cats = [
                    c for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit] if c != OTHER
                ]
            cats = [*cats, OTHER]
            categories[key] = cats
            for c in cats:
                col = f"{key}={c}"
                columns.append(col)
                column_feature[col] = key
        else:
            values = [_transform(float(r[key]), f) for r in rows if r.get(key) is not None]
            impute[key] = dec(sum(values) / len(values)) if values else dec(0.0)
            columns.append(key)
            column_feature[key] = key
    encoder: dict[str, Any] = {
        "columns": columns,
        "column_feature": column_feature,
        "categories": categories,
        "impute": impute,
        "transforms": {
            f["key"]: {"transform": f.get("transform", "none"), "clip": f.get("clip")}
            for f in spec_features
            if f["type"] != "onehot"
        },
    }
    raw = [encode(r, encoder) for r in rows]
    means: dict[str, str] = {}
    scales: dict[str, str] = {}
    for i, col in enumerate(columns):
        vals = [row[i] for row in raw]
        mean = sum(vals) / len(vals) if vals else 0.0
        var = sum((v - mean) ** 2 for v in vals) / len(vals) if vals else 0.0
        std = math.sqrt(var)
        means[col] = dec(mean)
        scales[col] = dec(std if std > 1e-12 else 1.0)
    encoder["means"] = means
    encoder["scales"] = scales
    return encoder


def encode(features: Mapping[str, Any], encoder: Mapping[str, Any]) -> list[float]:
    out: list[float] = []
    cats = encoder["categories"]
    chosen: dict[str, str] = {key: _category(features.get(key), values) for key, values in cats.items()}
    for col in encoder["columns"]:
        key = encoder["column_feature"][col]
        if key in cats:
            out.append(1.0 if col == f"{key}={chosen[key]}" else 0.0)
            continue
        value = features.get(key)
        if value is None:
            out.append(num(encoder["impute"][key]))
        else:
            out.append(_transform(float(value), {**encoder["transforms"][key], "type": "numeric"}))
    return out


def standardize(raw: Sequence[float], encoder: Mapping[str, Any]) -> list[float]:
    cols = encoder["columns"]
    return [(raw[i] - num(encoder["means"][c])) / num(encoder["scales"][c]) for i, c in enumerate(cols)]


def chosen_category(features: Mapping[str, Any], key: str, encoder: Mapping[str, Any]) -> str | None:
    values = encoder["categories"].get(key)
    return _category(features.get(key), values) if values is not None else None
