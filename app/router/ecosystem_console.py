# ruff: file-ignore[line-too-long]
"""Ecosystem console (Epic 79 story 79.1 / 79.2, FR-405 / FR-406). Reads need ``integration:view`` or
``integration:manage``; mutations need ``integration:manage`` and are human-only (403 ``human_only_action`` for API
keys / automated actors). Never returns secrets, webhook payloads or Copilot / portal conversation text."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.ecosystem_governance import (
    AccessDecisionIn,
    AccessReviewItemOut,
    AccessReviewOut,
    AccessReviewSettingsIn,
    AccessReviewSettingsOut,
    ConsoleSummary,
    DataFlowRow,
    InstallationRow,
    RevokeIn,
    ViolationRow,
)
from app.services import ecosystem_governance_service as svc

router = APIRouter(prefix="/ecosystem-console", tags=["ecosystem-console"])
DB = TenantDBSessionDep
View = Annotated[Principal, Depends(require_any("integration:view", "integration:manage"))]
Manage = Annotated[Principal, Depends(require("integration:manage"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/summary", response_model=ConsoleSummary)
async def summary(_: View, db: DB) -> ConsoleSummary:
    return await svc.console.summary(get_context(), db)


@router.get("/installations", response_model=list[InstallationRow])
async def installations(
    _: View,
    db: DB,
    kind: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[InstallationRow]:
    return await svc.console.installations(get_context(), db, kind=kind, status=status, q=q)


@router.get("/authorizations")
async def authorizations(
    _: View,
    db: DB,
    installation_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    return await svc.console.authorizations(get_context(), db, installation_id=installation_id, user_id=user_id)


@router.post("/authorizations/{authorization_id}/revoke", status_code=204)
async def revoke_authorization(authorization_id: uuid.UUID, body: RevokeIn, _: Manage, db: DB) -> Response:
    _human()
    from app.services import oauth_service

    revoke = getattr(oauth_service, "revoke_for", None) or getattr(oauth_service, "revoke_authorization", None)
    if revoke is not None:
        await revoke(get_context(), db, authorization_id, reason=body.reason)
    return Response(status_code=204)


@router.get("/data-flows", response_model=list[DataFlowRow])
async def data_flows(_: View, db: DB) -> list[DataFlowRow]:
    return await svc.data_flows.register(get_context(), db)


@router.get("/data-flows.csv")
async def data_flows_csv(_: View, db: DB) -> Response:
    rows = await svc.data_flows.register(get_context(), db)
    body = svc.data_flows.to_csv(rows)
    return Response(content=body, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=data-flows.csv"})


@router.get("/violations", response_model=list[ViolationRow])
async def violations(
    _: View,
    db: DB,
    kind: Annotated[str | None, Query()] = None,
) -> list[ViolationRow]:
    return await svc.violations.list_violations(get_context(), db, kind=kind)


@router.get("/test-results")
async def test_results(_: View, db: DB) -> list[dict[str, Any]]:
    return await svc.release_gate.latest_for_tenant(get_context(), db)


# ---------------------------------------------------------------- access reviews (79.2)
@router.get("/access-reviews/settings", response_model=AccessReviewSettingsOut)
async def access_review_settings(_: View, db: DB) -> AccessReviewSettingsOut:
    ctx = get_context()
    row = await svc.access_reviews.get_settings(db, ctx.tenant_id)  # type: ignore[arg-type]
    return AccessReviewSettingsOut(
        cadence_months=row.cadence_months,
        stale_after_days=row.stale_after_days,
        auto_revoke_stale=row.auto_revoke_stale,
        reviewer_role_key=row.reviewer_role_key,
    )


@router.put("/access-reviews/settings", response_model=AccessReviewSettingsOut)
async def set_access_review_settings(body: AccessReviewSettingsIn, p: Manage, db: DB) -> AccessReviewSettingsOut:
    _human()
    ctx = get_context()
    row = await svc.access_reviews.update_settings(
        db,
        ctx.tenant_id,  # type: ignore[arg-type]
        cadence_months=body.cadence_months,
        stale_after_days=body.stale_after_days,
        auto_revoke_stale=body.auto_revoke_stale,
        reviewer_role_key=body.reviewer_role_key,
        updated_by=getattr(p, "user_id", None),
    )
    return AccessReviewSettingsOut(
        cadence_months=row.cadence_months,
        stale_after_days=row.stale_after_days,
        auto_revoke_stale=row.auto_revoke_stale,
        reviewer_role_key=row.reviewer_role_key,
    )


@router.get("/access-reviews", response_model=list[AccessReviewOut])
async def list_reviews(_: View, db: DB) -> list[AccessReviewOut]:
    ctx = get_context()
    reviews = await svc.access_reviews.list_reviews(db, ctx.tenant_id)  # type: ignore[arg-type]
    return [
        AccessReviewOut(
            id=r.id, period_start=r.period_start, period_end=r.period_end, status=r.status,
            item_count=r.item_count, decided_count=r.decided_count, opened_at=r.opened_at, completed_at=r.completed_at,
        )
        for r in reviews
    ]


@router.get("/access-reviews/{review_id}/items", response_model=list[AccessReviewItemOut])
async def review_items(
    review_id: uuid.UUID,
    _: View,
    db: DB,
    stale_only: Annotated[bool, Query()] = False,
    kind: Annotated[str | None, Query()] = None,
) -> list[AccessReviewItemOut]:
    ctx = get_context()
    items = await svc.access_reviews.list_items(db, ctx.tenant_id, review_id, stale_only=stale_only, kind=kind)  # type: ignore[arg-type]
    return [
        AccessReviewItemOut(
            id=i.id, kind=i.kind, subject_id=i.subject_id, snapshot=i.snapshot, stale=i.stale,
            decision=i.decision, note=i.note,
        )
        for i in items
    ]


@router.post("/access-reviews/items/{item_id}/decide", response_model=AccessReviewItemOut)
async def decide_item(item_id: uuid.UUID, body: AccessDecisionIn, p: Manage, db: DB) -> AccessReviewItemOut:
    _human()
    ctx = get_context()
    item = await svc.access_reviews.decide_item(
        db, ctx.tenant_id, item_id, decision=body.decision, decided_by=getattr(p, "user_id", None), note=body.note  # type: ignore[arg-type]
    )
    return AccessReviewItemOut(
        id=item.id, kind=item.kind, subject_id=item.subject_id, snapshot=item.snapshot, stale=item.stale,
        decision=item.decision, note=item.note,
    )


@router.post("/access-reviews/{review_id}/complete", response_model=AccessReviewOut)
async def complete_review(review_id: uuid.UUID, p: Manage, db: DB) -> AccessReviewOut:
    _human()
    ctx = get_context()
    r = await svc.access_reviews.complete_review(db, ctx.tenant_id, review_id, completed_by=getattr(p, "user_id", None))  # type: ignore[arg-type]
    return AccessReviewOut(
        id=r.id, period_start=r.period_start, period_end=r.period_end, status=r.status,
        item_count=r.item_count, decided_count=r.decided_count, opened_at=r.opened_at, completed_at=r.completed_at,
    )


@router.get("/feature-readiness")
async def feature_readiness(_: View, db: DB) -> list[dict[str, Any]]:
    rows = await svc.enablement.readiness(get_context(), db)
    return [r.model_dump() for r in rows]


__all__ = ["router"]
