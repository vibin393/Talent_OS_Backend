# ruff: noqa: E501, D401, RUF001
"""Scoring (Epic 48 A5, FR-241..243). An LLM never produces or adjusts a score.

``score(db, kind, subject_ids=None, subject_kind=None)`` loads the active model and the extractor output with
``as_of = now`` and upserts ``ml_predictions`` (UNIQUE ``(subject_kind, subject_id, model_id)``):

* logistic: ``p = sigmoid(b + sum(w_i z_i))``; contribution = ``w_i z_i`` (log-odds);
* ridge: ``y = b + sum(w_i z_i)``; days = ``exp(y) - 1``; interval ``exp(y + q20) - 1 .. exp(y + q80) - 1``;
  the displayed contribution in days is ``exp(y) - exp(y - c_i)``.

Contributions are labelled from the spec; sensitive values are bucketed (the compensation change is shown as a
"+20-30 %" band, never an amount). When the band differs from the previous prediction of the same subject and kind
(any earlier model), ``PredictionBandChanged`` is emitted (dispatched after commit).
"""

import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.analytics import PredictionBandChanged
from app.models.analytics import MlModel, MlPrediction
from app.services.analytics import bands, governance
from app.services.analytics.features.specs import SPECS, FeatureDef

FOUR = Decimal("0.0001")
ONE = Decimal("0.1")


def _q(value: float, exp: Decimal) -> Decimal:
    return Decimal(repr(float(value))).quantize(exp, rounding=ROUND_HALF_UP)


def pct_band(value: float) -> str:
    """A compensation change as a 10-point band ("+20-30 %"), never the amount."""
    lo = math.floor(value / 10.0) * 10
    hi = lo + 10
    sign = "+" if lo >= 0 else "-" if hi <= 0 else ""
    if sign == "-":
        return f"-{abs(hi)}–{abs(lo)} %"
    if sign == "":
        return f"{lo}–+{hi} %"
    return f"+{lo}–{hi} %"


def value_display(f: FeatureDef, value: Any, category: str | None = None) -> str:
    if f.type == "onehot":
        shown = category if category not in (None, "other") else None
        return (str(shown).title() if f.key == "city" else str(shown).replace("_", " ")) if shown else "Other"
    if value is None:
        return "Not recorded"
    v = float(value)
    if f.display == "flag" or f.type == "binary":
        return "Yes" if v >= 0.5 else "No"
    if f.display == "pct_band":
        return pct_band(v)
    if f.display == "pct":
        return f"{round(v * 100)} %"
    if f.display == "ratio":
        return f"{v:.1f}×"
    if f.display in ("count", "category"):
        return f"{round(v)}"
    unit = f" {f.unit}" if f.unit else ""
    return f"{round(v)}{unit}" if abs(v) >= 10 else f"{v:.1f}".rstrip("0").rstrip(".") + unit


def contributions(
    kind: str, params: Mapping[str, Any], features: Mapping[str, Any], pred: Any
) -> list[dict[str, Any]]:
    from app.services.analytics.models import scaler

    spec = SPECS[kind]
    out: list[dict[str, Any]] = []
    for f in spec.features:
        c = float(pred.contributions.get(f.key, 0.0))
        if spec.task == "regression":
            days = math.exp(pred.linear) - math.exp(pred.linear - c)
            display = f"{'+' if days >= 0 else '−'}{abs(round(days))} days"
        else:
            display = f"{'+' if c >= 0 else '−'}{abs(c):.2f}"
        category = scaler.chosen_category(features, f.key, params["encoder"]) if f.type == "onehot" else None
        out.append(
            {
                "key": f.key,
                "label": f.label,
                "value_display": value_display(f, features.get(f.key), category),
                "contribution": str(_q(c, Decimal("0.000001"))),
                "direction": "up" if c >= 0 else "down",
                "display": display,
                "_abs": abs(c),
            }
        )
    out.sort(key=lambda d: (-d["_abs"], d["key"]))
    for d in out:
        d.pop("_abs")
    return out


async def _previous_bands(
    db: AsyncSession, kind: str, keys: Sequence[tuple[str, uuid.UUID]]
) -> dict[tuple[str, uuid.UUID], tuple[uuid.UUID, str | None]]:
    """(subject_kind, subject_id) -> (model id, band) of the latest prediction of ``kind`` (any model)."""
    if not keys:
        return {}
    ids = list({k[1] for k in keys})
    rows = await db.execute(
        select(MlPrediction.subject_kind, MlPrediction.subject_id, MlPrediction.model_id, MlPrediction.band)
        .where(MlPrediction.kind == kind, MlPrediction.subject_id.in_(ids))
        .order_by(MlPrediction.computed_at.asc())
    )
    out: dict[tuple[str, uuid.UUID], tuple[uuid.UUID, str | None]] = {}
    for sk, sid, mid, band in rows:
        out[sk, sid] = (mid, band)
    return out


async def score(
    db: AsyncSession,
    kind: str,
    subject_ids: Sequence[uuid.UUID] | None = None,
    *,
    subject_kind: str | None = None,
    now: datetime | None = None,
) -> int:
    """Score the live subjects of ``kind`` (all, or ``subject_ids``); returns the number of predictions written."""
    from app.services.analytics import features
    from app.services.analytics import models as ml

    settings = await governance.settings_row(db)
    if not governance.enabled(settings, kind):
        return 0
    model = await governance.active_model(db, kind)
    if model is None:
        return 0
    now = now or datetime.now(UTC)
    rows = await features.scoring_rows(db, kind, now, subject_ids, subject_kind)
    if not rows:
        return 0
    thresholds = governance.thresholds(settings, kind)
    previous = await _previous_bands(db, kind, [(r.subject_kind, r.subject_id) for r in rows])
    existing = {
        (p.subject_kind, p.subject_id): p
        for p in (
            await db.execute(
                select(MlPrediction).where(
                    MlPrediction.model_id == model.id,
                    MlPrediction.subject_id.in_([r.subject_id for r in rows]),
                )
            )
        ).scalars()
    }
    events: list[PredictionBandChanged] = []
    for r in rows:
        pred = ml.predict(model.params, r.features)
        contribs = contributions(kind, model.params, r.features, pred)
        if pred.probability is not None:
            score_value = _q(pred.probability, FOUR)
            band = bands.band_for(pred.probability, thresholds)
            interval = None
            days = None
        else:
            score_value = _q(max(pred.days or 0.0, 0.0), FOUR)
            band = None
            days = _q(max(pred.days or 0.0, 0.0), ONE)
            interval = {
                "p20": str(_q(max(pred.p20 or 0.0, 0.0), ONE)),
                "p80": str(_q(max(pred.p80 or 0.0, 0.0), ONE)),
            }
        key = (r.subject_kind, r.subject_id)
        row = existing.get(key)
        if row is None:
            row = MlPrediction(
                id=uuid.uuid4(),
                tenant_id=model.tenant_id,
                model_id=model.id,
                kind=kind,
                subject_kind=r.subject_kind,
                subject_id=r.subject_id,
            )
            db.add(row)
        changed = row.contributions != contribs if row.contributions is not None else True
        row.candidate_id = r.candidate_id
        row.score = score_value
        row.predicted_days = days
        row.band = band
        row.interval = interval
        row.contributions = contribs
        row.computed_at = now
        if changed or row.explanation_status is None:
            row.explanation_status = "none"
            row.explanation_text = None
            row.explanation_ai_action_id = None
        prev = previous.get(key)
        from_band = prev[1] if prev is not None else None
        if band is not None and band != from_band:
            events.append(
                PredictionBandChanged(
                    tenant_id=model.tenant_id,
                    prediction_id=row.id,
                    model_kind=kind,
                    subject_kind=r.subject_kind,
                    subject_id=r.subject_id,
                    from_band=from_band,
                    to_band=band,
                )
            )
    await db.flush()
    for event in events:
        emit(db, event)
    return len(rows)


async def score_all(db: AsyncSession) -> dict[str, int]:
    out: dict[str, int] = {}
    for kind in SPECS:
        out[kind] = await score(db, kind)
    return out


async def model_for(db: AsyncSession, model_id: uuid.UUID) -> MlModel | None:
    return await db.get(MlModel, model_id)
