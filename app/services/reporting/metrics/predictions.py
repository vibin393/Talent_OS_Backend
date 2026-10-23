# ruff: file-ignore[line-too-long, lowercase-imported-as-non-lowercase]
"""Predictions dashboard tiles (Epic 52 A4; Epic 48 data). Every tile is a **supporting signal**, never a decision
(NFR-37): the tile payload carries ``badge``. ``attrition`` appears only with ``prediction:read_attrition`` (owner rule
``analytics.governance.can_read_kind``, reached through ``core.prediction_port``: no service imports analytics). Subjects are limited to those the caller can read (demand / offer scope via the
demands' own visibility rule, placements via the placements' rule). Only the latest prediction per subject and kind
counts."""

from typing import Any

from sqlalchemy import and_, func, or_, select

from app.core.prediction_port import prediction_reader
from app.models.analytics import MlPrediction
from app.services.reporting.common import demand_scope
from app.services.reporting.metrics import TileContext, TileData, pct
from app.services.reporting.metrics.p2common import placement_scope
from app.services.reporting.metrics.p3common import SUPPORTING_SIGNAL, need

KIND_LABELS = {"time_to_fill": "Time to fill", "offer_drop": "Offer drop-out", "attrition": "Attrition"}
BANDS = ("low", "medium", "high")


def _kinds(t: TileContext) -> list[str]:
    reader = prediction_reader()
    return [k for k in KIND_LABELS if reader.can_read_kind(t.principal, k)]


def _subject_scope(t: TileContext) -> Any:
    """Predictions whose subject the caller can read."""
    from app.models.demands import Demand
    from app.models.offers import Offer
    from app.services.reporting import p2tables as T

    p = MlPrediction
    parts = []
    d = demand_scope(t.principal, "demand:read")
    parts.append(
        and_(
            p.subject_kind == "demand",
            p.subject_id.in_(select(Demand.id).where(d) if d is not None else select(Demand.id)),
        )
    )
    o = demand_scope(t.principal, "offer:read")
    parts.append(
        and_(
            p.subject_kind == "offer",
            p.subject_id.in_(
                select(Offer.id).where(
                    Offer.demand_id.in_(select(Demand.id).where(o) if o is not None else select(Demand.id))
                )
            ),
        )
    )
    if "placement:read" in t.principal.permissions:
        ps = placement_scope(t.principal)
        pl = T.placements.c  # placement_scope is built on the read-model projection (one FROM object)
        parts.append(
            and_(
                p.subject_kind == "placement",
                p.subject_id.in_(select(pl.id).where(ps) if ps is not None else select(pl.id)),
            )
        )
    return or_(*parts)


def _latest(t: TileContext, kinds: list[str]) -> Any:
    p = MlPrediction
    ranked = (
        select(
            p.kind, p.subject_kind, p.subject_id, p.band, p.computed_at,
            func.row_number().over(partition_by=(p.kind, p.subject_kind, p.subject_id), order_by=p.computed_at.desc()).label("rn"),
        )
        .where(p.kind.in_(kinds), _subject_scope(t))
        .subquery()
    )  # fmt: skip
    return select(ranked).where(ranked.c.rn == 1).subquery()


async def risk_band_distribution(t: TileContext) -> TileData:
    need(t, "ml_predictions")
    kinds = _kinds(t)
    latest = _latest(t, kinds)
    rows_db = (
        await t.db.execute(
            select(latest.c.kind, latest.c.band, func.count())
            .where(latest.c.band.is_not(None))
            .group_by(latest.c.kind, latest.c.band)
        )
    ).all()
    by: dict[str, dict[str, int]] = {k: dict.fromkeys(BANDS, 0) for k in kinds if k != "time_to_fill"}
    for kind, band, n in rows_db:
        if kind in by:
            by[kind][band] = int(n)
    rows = [{"key": k, "label": KIND_LABELS[k], **v} for k, v in by.items()]
    return {
        "rows": rows,
        "series_keys": [{"key": b, "label": b.capitalize()} for b in BANDS],
        "badge": SUPPORTING_SIGNAL,
    }


async def model_status(t: TileContext) -> TileData:
    cards = await prediction_reader().model_cards(t.ctx, t.db)
    rows = []
    for c in cards:
        a = c.active
        rows.append({
            "kind": c.kind_label, "status": c.status.replace("_", " ").capitalize(),
            "trained_at": a.trained_at.isoformat() if a and a.trained_at else None,
            "n_samples": a.n_samples if a else None, "n_minority": a.n_minority if a else None,
            "metric": next(iter(a.metrics.items()), (None, None))[1] if a and a.metrics else None,
            "baseline": next(iter(a.baseline.items()), (None, None))[1] if a and a.baseline else None,
        })  # fmt: skip
    return {"value": len(rows), "rows": rows, "badge": SUPPORTING_SIGNAL}


async def high_risk_offers_with_intervention(t: TileContext) -> TileData:
    """SM-26 (second half): accepted offers / pre-joining placements whose latest offer drop-out band is ``high`` and
    that have a Task on the subject created at or after that prediction (the intervention)."""
    need(t, "ml_predictions")
    from app.models.tasks import Task

    if "offer_drop" not in _kinds(t):
        return {"value": None, "n": 0, "badge": SUPPORTING_SIGNAL}
    latest = _latest(t, ["offer_drop"])
    high = select(latest).where(latest.c.band == "high").subquery()
    intervened = (
        select(Task.id)
        .where(
            Task.entity_type == high.c.subject_kind,
            Task.entity_id == high.c.subject_id,
            Task.created_at >= high.c.computed_at,
        )
        .exists()
    )
    total, with_task = (
        await t.db.execute(select(func.count(), func.count().filter(intervened)).select_from(high))
    ).one()
    return {
        "value": pct(int(with_task), int(total)),
        "unit": "%",
        "n": int(total),
        "rows": [
            {"key": "high_risk", "label": "High risk", "value": int(total)},
            {"key": "with_intervention", "label": "With an intervention task", "value": int(with_task)},
        ],
        "badge": SUPPORTING_SIGNAL,
    }
