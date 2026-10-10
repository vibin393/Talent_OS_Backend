# ruff: noqa: E501
"""Predictive analytics (Epic 48, FR-241..245): ``/api/v1``. Internal Users only (no portal router exists).

* ``GET /predictions`` / ``GET /predictions/batch`` -- ``prediction:read`` (time-to-fill, offer-drop) and / or
  ``prediction:read_attrition`` (attrition), plus read access to the subject. Without
  ``prediction:read_attrition`` the attrition entry is absent from every response.
* ``POST /predictions/{id}/explanation`` -- same permission as the read; lazy, cached AI explanation.
* ``GET /analytics/models`` -- ``model:manage`` (full card) or ``prediction:read`` (reduced card).
* ``POST /analytics/models/{kind}/train`` -- ``model:manage``: queue a retrain now (outside the nightly window).
* ``GET|PUT /settings/analytics`` -- ``model:manage``.

Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.analytics import (
    AnalyticsSettingsIn,
    AnalyticsSettingsOut,
    BatchOut,
    ExplanationOut,
    ModelCardsOut,
    ModelKind,
    PredictionsOut,
    TrainQueuedOut,
)
from app.services.analytics import explain, governance, reads

router = APIRouter(tags=["analytics"])
DB = TenantDBSessionDep
AnyRead = Annotated[Principal, Depends(require_any("prediction:read", "prediction:read_attrition"))]
CardRead = Annotated[
    Principal, Depends(require_any("model:manage", "prediction:read", "prediction:read_attrition"))
]
Manage = Annotated[Principal, Depends(require("model:manage"))]
SubjectKindQ = Literal["demand", "offer", "placement"]


@router.get("/predictions", response_model=PredictionsOut, response_model_exclude_none=False)
async def get_predictions(
    _: AnyRead,
    db: DB,
    subject_kind: Annotated[SubjectKindQ, Query()],
    subject_id: Annotated[uuid.UUID, Query()],
) -> PredictionsOut:
    return await reads.predictions_for(get_context(), db, subject_kind, subject_id)


@router.get("/predictions/batch", response_model=BatchOut)
async def get_prediction_badges(
    _: AnyRead,
    db: DB,
    subject_kind: Annotated[SubjectKindQ, Query()],
    ids: Annotated[list[uuid.UUID], Query(max_length=reads.MAX_BATCH)],
) -> BatchOut:
    return await reads.batch(get_context(), db, subject_kind, ids)


@router.post("/predictions/{prediction_id}/explanation", response_model=ExplanationOut)
async def explain_prediction(_: AnyRead, db: DB, prediction_id: uuid.UUID) -> ExplanationOut:
    return await explain.explain(get_context(), db, prediction_id)


@router.get("/analytics/models", response_model=ModelCardsOut)
async def list_model_cards(
    _: CardRead, db: DB, kind: Annotated[ModelKind | None, Query()] = None
) -> ModelCardsOut:
    return ModelCardsOut(cards=await governance.model_cards(get_context(), db, kind))


@router.post(
    "/analytics/models/{kind}/train", response_model=TrainQueuedOut, status_code=status.HTTP_202_ACCEPTED
)
async def retrain_now(_: Manage, kind: ModelKind) -> TrainQueuedOut:  # noqa: ARG001 - every enabled kind retrains
    from app.tasks import analytics as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.train_tenant, str(get_context().tenant_id), True)
    return TrainQueuedOut(queued=True)


@router.get("/settings/analytics", response_model=AnalyticsSettingsOut)
async def get_analytics_settings(_: Manage, db: DB) -> AnalyticsSettingsOut:
    return await governance.get_settings(get_context(), db)


@router.put("/settings/analytics", response_model=AnalyticsSettingsOut)
async def update_analytics_settings(_: Manage, db: DB, data: AnalyticsSettingsIn) -> AnalyticsSettingsOut:
    return await governance.update_settings(get_context(), db, data)
