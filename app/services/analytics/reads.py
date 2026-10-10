# ruff: noqa: E501, D401
"""Prediction reads (Epic 48 A5, FR-241..243, FR-245). One indexed query per subject; no model load.

Permissions per kind: ``time_to_fill`` (demand) and ``offer_drop`` (offer / placement) need ``prediction:read``;
**``attrition`` needs ``prediction:read_attrition``** -- without it the attrition entry is absent from every
response (no key, no null, no count). The subject must also be readable by the caller (the owning domain's
access check; 404 otherwise). A disabled kind or a kind without an active model returns a status entry
(``disabled`` / ``insufficient_data`` with progress / ``no_model``). Only live subjects show predictions (a filled
Demand or an ended Placement keeps its last prediction for history, but reads omit it).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError, Forbidden, NotFound
from app.models.analytics import MlModel, MlPrediction
from app.schemas.analytics import (
    BatchItem,
    BatchOut,
    ContributionOut,
    Interval,
    PredictionEntry,
    PredictionsOut,
)
from app.services.analytics import governance
from app.services.analytics.features.specs import KIND_LABELS

KINDS_BY_SUBJECT: dict[str, tuple[str, ...]] = {
    "demand": ("time_to_fill",),
    "offer": ("offer_drop",),
    "placement": ("offer_drop", "attrition"),
}
MAX_BATCH = 200


# ---------------------------------------------------------------- access / liveness
async def subject_readable(
    ctx: RequestContext, db: AsyncSession, subject_kind: str, subject_id: uuid.UUID
) -> bool:
    try:
        if subject_kind == "demand":
            from app.services.demands import demand_access

            return await demand_access(ctx, db, subject_id, False)
        if subject_kind == "offer":
            from app.services.offers import offer_access

            return await offer_access(ctx, db, subject_id, False)
        if subject_kind == "placement":
            from app.services.placements.lists import visible

            await visible(ctx, db, subject_id)
            return True
    except DomainError:
        return False
    return False


async def live_kinds(db: AsyncSession, subject_kind: str, subject_id: uuid.UUID) -> set[str]:
    """Kinds whose scored set currently contains the subject."""
    if subject_kind == "demand":
        from app.models.demands import Demand

        status = (await db.execute(select(Demand.status).where(Demand.id == subject_id))).scalar_one_or_none()
        return {"time_to_fill"} if status == "open" else set()
    if subject_kind == "offer":
        from app.models.offers import Offer
        from app.models.placements import Placement

        status = (await db.execute(select(Offer.status).where(Offer.id == subject_id))).scalar_one_or_none()
        if status != "accepted":
            return set()
        placed = (
            await db.execute(select(Placement.id).where(Placement.offer_id == subject_id).limit(1))
        ).first()
        return set() if placed is not None else {"offer_drop"}
    from app.models.placements import Placement

    status = (
        await db.execute(select(Placement.status).where(Placement.id == subject_id))
    ).scalar_one_or_none()
    if status in ("pre_joining", "joining_confirmed"):
        return {"offer_drop"}
    if status == "active":
        return {"attrition"}
    return set()


def visible_kinds(ctx: RequestContext, subject_kind: str) -> list[str]:
    return [k for k in KINDS_BY_SUBJECT[subject_kind] if governance.can_read_kind(ctx.principal, k)]


# ---------------------------------------------------------------- entries
def _contributions(raw: Sequence[dict[str, Any]]) -> list[ContributionOut]:
    return [
        ContributionOut(
            **{k: c[k] for k in ("key", "label", "value_display", "contribution", "direction", "display")}
        )
        for c in raw or []
    ]


def entry_from(p: MlPrediction, model: MlModel | None) -> PredictionEntry:
    return PredictionEntry(
        kind=p.kind,  # type: ignore[arg-type]
        kind_label=KIND_LABELS[p.kind],
        status="ok",
        prediction_id=p.id,
        model_id=p.model_id,
        model_version=model.version if model is not None else None,
        computed_at=p.computed_at,
        score=str(p.score),
        predicted_days=str(p.predicted_days) if p.predicted_days is not None else None,
        interval=Interval(**p.interval) if p.interval else None,
        band=p.band,  # type: ignore[arg-type]
        contributions=_contributions(p.contributions),
        explanation_status=p.explanation_status,  # type: ignore[arg-type]
        explanation_text=p.explanation_text if p.explanation_status == "ok" else None,
    )


async def _status_entry(db: AsyncSession, kind: str, enabled: bool) -> PredictionEntry | None:
    """A status-only entry, or None when a model is active (the caller then reads the prediction)."""
    if not enabled:
        return PredictionEntry(kind=kind, kind_label=KIND_LABELS[kind], status="disabled")  # type: ignore[arg-type]
    if await governance.active_model(db, kind) is not None:
        return None
    latest = await governance.latest_model(db, kind)
    progress = governance.progress_of(latest)
    if progress is not None:
        return PredictionEntry(
            kind=kind, kind_label=KIND_LABELS[kind], status="insufficient_data", progress=progress
        )  # type: ignore[arg-type]
    return PredictionEntry(kind=kind, kind_label=KIND_LABELS[kind], status="no_model")  # type: ignore[arg-type]


async def _latest(
    db: AsyncSession, kind: str, subject_kind: str, subject_id: uuid.UUID
) -> tuple[MlPrediction, MlModel] | None:
    row = (
        await db.execute(
            select(MlPrediction, MlModel)
            .join(MlModel, MlModel.id == MlPrediction.model_id)
            .where(
                MlPrediction.subject_kind == subject_kind,
                MlPrediction.subject_id == subject_id,
                MlPrediction.kind == kind,
                MlModel.status == "active",
            )
            .order_by(MlPrediction.computed_at.desc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def predictions_for(
    ctx: RequestContext, db: AsyncSession, subject_kind: str, subject_id: uuid.UUID
) -> PredictionsOut:
    kinds = visible_kinds(ctx, subject_kind)
    if not kinds:
        raise Forbidden(
            "You do not have permission to view predictions",
            code="forbidden",
            details={"required_permission": governance.READ},
        )
    if not await subject_readable(ctx, db, subject_kind, subject_id):
        raise NotFound("Resource not found")
    settings = await governance.settings_row(db, ctx.tenant_id)
    live = await live_kinds(db, subject_kind, subject_id)
    entries: list[PredictionEntry] = []
    for kind in kinds:
        if kind not in live:
            continue
        status = await _status_entry(db, kind, governance.enabled(settings, kind))
        if status is not None:
            entries.append(status)
            continue
        found = await _latest(db, kind, subject_kind, subject_id)
        if found is None:
            entries.append(PredictionEntry(kind=kind, kind_label=KIND_LABELS[kind], status="no_model"))  # type: ignore[arg-type]
        else:
            entries.append(entry_from(*found))
    return PredictionsOut(subject_kind=subject_kind, subject_id=subject_id, predictions=entries)  # type: ignore[arg-type]


async def batch(
    ctx: RequestContext, db: AsyncSession, subject_kind: str, ids: Sequence[uuid.UUID]
) -> BatchOut:
    kinds = visible_kinds(ctx, subject_kind)
    if not kinds:
        raise Forbidden(
            "You do not have permission to view predictions",
            code="forbidden",
            details={"required_permission": governance.READ},
        )
    wanted = list(dict.fromkeys(ids))[:MAX_BATCH]
    settings = await governance.settings_row(db, ctx.tenant_id)
    active = {
        k: m
        for k in kinds
        if governance.enabled(settings, k) and (m := await governance.active_model(db, k)) is not None
    }
    if not wanted or not active:
        return BatchOut(items=[])
    rows = await db.execute(
        select(MlPrediction).where(
            MlPrediction.subject_kind == subject_kind,
            MlPrediction.subject_id.in_(wanted),
            MlPrediction.model_id.in_([m.id for m in active.values()]),
        )
    )
    items: list[BatchItem] = []
    readable: dict[uuid.UUID, bool] = {}
    for p in rows.scalars():
        if p.subject_id not in readable:
            readable[p.subject_id] = await subject_readable(ctx, db, subject_kind, p.subject_id)
        if not readable[p.subject_id] or p.kind not in await live_kinds(db, subject_kind, p.subject_id):
            continue
        items.append(
            BatchItem(
                subject_id=p.subject_id,
                kind=p.kind,  # type: ignore[arg-type]
                status="ok",
                band=p.band,  # type: ignore[arg-type]
                score=str(p.score),
                predicted_days=str(p.predicted_days) if p.predicted_days is not None else None,
                interval=Interval(**p.interval) if p.interval else None,
            )
        )
    return BatchOut(items=items)


async def readable_prediction(
    ctx: RequestContext, db: AsyncSession, prediction_id: uuid.UUID
) -> MlPrediction:
    """The prediction when the caller may read its kind and its subject (404 otherwise; 403 without the key)."""
    p = await db.get(MlPrediction, prediction_id)
    if p is None:
        raise NotFound("Resource not found")
    if not governance.can_read_kind(ctx.principal, p.kind):
        # Attrition stays invisible: same 404 as an unknown id.
        raise NotFound("Resource not found")
    if not await subject_readable(ctx, db, p.subject_kind, p.subject_id):
        raise NotFound("Resource not found")
    return p
