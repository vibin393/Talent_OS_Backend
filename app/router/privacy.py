"""Privacy endpoints (Epic 12): consent + AI-processing basis, DSRs, legal holds, retention, support access.

Routers only: every endpoint calls one service function (AD-1). The DB session is the caller's RLS-scoped
tenant session, so another tenant's ids are simply not found (404).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.platform import SupportGrantOut
from app.schemas.privacy import (
    AIBasisOut,
    AIBasisUpdate,
    ClassExpiryPreviewOut,
    ConsentOut,
    ConsentRequestIn,
    ConsentRequestResult,
    DsrCreate,
    DsrDetailOut,
    DsrExecuteIn,
    DsrFilters,
    DsrOut,
    DsrTransitionIn,
    DsrUpdate,
    ErasureRunOut,
    LegalHoldCreate,
    LegalHoldOut,
    NoticeCreate,
    NoticeOut,
    RetentionBindingOut,
    RetentionClassCreate,
    RetentionClassOut,
    RetentionClassUpdate,
    RetentionExecuteIn,
    RetentionItemOut,
    RetentionOverrideIn,
    RetentionPolicyOut,
    RetentionPolicyUpdate,
    RetentionPreviewIn,
    RetentionRunOut,
)
from app.services import privacy, support_access_service

router = APIRouter(prefix="/privacy", tags=["privacy"])
consents_router = APIRouter(tags=["privacy"])

PrivacyManage = Annotated[Principal, Depends(require("privacy:manage"))]
ConsentRead = Annotated[Principal, Depends(require("consent:read"))]
ConsentWrite = Annotated[Principal, Depends(require("consent:write"))]
DsrRead = Annotated[Principal, Depends(require("dsr:read"))]
DsrProcess = Annotated[Principal, Depends(require("dsr:process"))]
RetentionManage = Annotated[Principal, Depends(require("retention:manage"))]
SupportApprove = Annotated[Principal, Depends(require("support_access:approve"))]
Paging = Annotated[PageParams, Depends()]


# ================================================================ basis / consent
@router.get("/ai-basis", response_model=AIBasisOut)
async def get_ai_basis(_: PrivacyManage, db: TenantDBSessionDep) -> AIBasisOut:
    return await privacy.basis_out(get_context(), db)


@router.put("/ai-basis", response_model=AIBasisOut)
async def put_ai_basis(body: AIBasisUpdate, _: PrivacyManage, db: TenantDBSessionDep) -> AIBasisOut:
    return await privacy.set_basis(get_context(), db, body.basis)


@router.post("/consent-requests", response_model=ConsentRequestResult)
async def consent_requests(body: ConsentRequestIn, _: ConsentWrite, db: TenantDBSessionDep) -> ConsentRequestResult:
    return await privacy.request_consents(get_context(), db, body.candidate_ids, body.purpose, body.client_id)


@router.get("/consent-notices", response_model=list[NoticeOut])
async def list_notices(
    _: PrivacyManage, db: TenantDBSessionDep, purpose: Annotated[str | None, Query(max_length=30)] = None
) -> list[NoticeOut]:
    return await privacy.consent.list_notices(get_context(), db, purpose)


@router.post("/consent-notices", response_model=NoticeOut, status_code=status.HTTP_201_CREATED)
async def create_notice(body: NoticeCreate, _: PrivacyManage, db: TenantDBSessionDep) -> NoticeOut:
    return await privacy.consent.create_notice(get_context(), db, body)


@router.post("/consent-notices/{notice_id}/activate", response_model=NoticeOut)
async def activate_notice(notice_id: uuid.UUID, _: PrivacyManage, db: TenantDBSessionDep) -> NoticeOut:
    return await privacy.consent.activate_notice(get_context(), db, notice_id)


@consents_router.get("/candidates/{candidate_id}/consents", response_model=list[ConsentOut])
async def candidate_consents(candidate_id: uuid.UUID, _: ConsentRead, db: TenantDBSessionDep) -> list[ConsentOut]:
    return await privacy.consent.list_candidate_consents(get_context(), db, candidate_id)


@consents_router.post("/consents/{consent_id}/withdraw", response_model=ConsentOut)
async def withdraw_consent(consent_id: uuid.UUID, _: ConsentWrite, db: TenantDBSessionDep) -> ConsentOut:
    return await privacy.consent.withdraw_by_id(get_context(), db, consent_id)


# ================================================================ DSRs
@router.get("/dsrs", response_model=Page[DsrOut])
async def list_dsrs(
    _: DsrRead, db: TenantDBSessionDep, filters: Annotated[DsrFilters, Depends()], params: Paging
) -> Page[DsrOut]:
    return await privacy.dsr.list_dsrs(get_context(), db, filters, params)


@router.post("/dsrs", response_model=DsrOut, status_code=status.HTTP_201_CREATED)
async def create_dsr(body: DsrCreate, _: DsrProcess, db: TenantDBSessionDep) -> DsrOut:
    return await privacy.dsr.create_dsr(get_context(), db, body)


@router.get("/dsrs/{dsr_id}", response_model=DsrDetailOut)
async def get_dsr(dsr_id: uuid.UUID, _: DsrRead, db: TenantDBSessionDep) -> DsrDetailOut:
    return await privacy.dsr.get_dsr(get_context(), db, dsr_id)


@router.patch("/dsrs/{dsr_id}", response_model=DsrDetailOut)
async def update_dsr(dsr_id: uuid.UUID, body: DsrUpdate, _: DsrProcess, db: TenantDBSessionDep) -> DsrDetailOut:
    return await privacy.dsr.update_dsr(get_context(), db, dsr_id, body)


@router.post("/dsrs/{dsr_id}/verify-identity", response_model=DsrDetailOut)
async def verify_identity(dsr_id: uuid.UUID, _: DsrProcess, db: TenantDBSessionDep) -> DsrDetailOut:
    return await privacy.dsr.verify_identity(get_context(), db, dsr_id)


@router.post("/dsrs/{dsr_id}/transition", response_model=DsrDetailOut)
async def transition_dsr(
    dsr_id: uuid.UUID, body: DsrTransitionIn, _: DsrProcess, db: TenantDBSessionDep
) -> DsrDetailOut:
    return await privacy.dsr.transition(
        get_context(), db, dsr_id, body.to, refusal_reason=body.refusal_reason, note=body.note
    )


@router.post("/dsrs/{dsr_id}/execute", response_model=DsrDetailOut)
async def execute_dsr(dsr_id: uuid.UUID, body: DsrExecuteIn, _: DsrProcess, db: TenantDBSessionDep) -> DsrDetailOut:
    return await privacy.dsr.execute(get_context(), db, dsr_id, body)


@router.get("/erasure-runs/{run_id}", response_model=ErasureRunOut)
async def get_erasure_run(run_id: uuid.UUID, _: DsrRead, db: TenantDBSessionDep) -> ErasureRunOut:
    return await privacy.erasure.run_out(get_context(), db, run_id)


@router.post("/erasure-runs/{run_id}/retry", response_model=ErasureRunOut)
async def retry_erasure_run(run_id: uuid.UUID, _: DsrProcess, db: TenantDBSessionDep) -> ErasureRunOut:
    return await privacy.erasure.retry(get_context(), db, run_id)


# ================================================================ legal holds
@router.get("/legal-holds", response_model=list[LegalHoldOut])
async def list_holds(
    _: PrivacyManage, db: TenantDBSessionDep, active: Annotated[bool, Query()] = True
) -> list[LegalHoldOut]:
    return await privacy.dsr.list_holds(get_context(), db, active)


@router.post("/legal-holds", response_model=LegalHoldOut, status_code=status.HTTP_201_CREATED)
async def place_hold(body: LegalHoldCreate, _: PrivacyManage, db: TenantDBSessionDep) -> LegalHoldOut:
    return await privacy.dsr.place_hold(get_context(), db, body)


@router.post("/legal-holds/{hold_id}/release", response_model=LegalHoldOut)
async def release_hold(hold_id: uuid.UUID, _: PrivacyManage, db: TenantDBSessionDep) -> LegalHoldOut:
    return await privacy.dsr.release_hold(get_context(), db, hold_id)


# ================================================================ retention
@router.get("/retention/policy", response_model=RetentionPolicyOut)
async def get_retention_policy(_: RetentionManage, db: TenantDBSessionDep) -> RetentionPolicyOut:
    return await privacy.retention.get_policy(get_context(), db)


@router.put("/retention/policy", response_model=RetentionPolicyOut)
async def put_retention_policy(
    body: RetentionPolicyUpdate, _: RetentionManage, db: TenantDBSessionDep
) -> RetentionPolicyOut:
    return await privacy.retention.update_policy(get_context(), db, body)


@router.post("/retention/preview", response_model=RetentionRunOut, status_code=status.HTTP_201_CREATED)
async def retention_preview(body: RetentionPreviewIn, _: RetentionManage, db: TenantDBSessionDep) -> RetentionRunOut:
    return await privacy.retention.preview(get_context(), db, body.candidate_inactive_months)


@router.get("/retention/runs", response_model=Page[RetentionRunOut])
async def retention_runs(_: RetentionManage, db: TenantDBSessionDep, params: Paging) -> Page[RetentionRunOut]:
    return await privacy.retention.list_runs(get_context(), db, params)


@router.get("/retention/runs/{run_id}", response_model=RetentionRunOut)
async def retention_run(run_id: uuid.UUID, _: RetentionManage, db: TenantDBSessionDep) -> RetentionRunOut:
    return await privacy.retention.get_run(get_context(), db, run_id)


@router.get("/retention/runs/{run_id}/items", response_model=Page[RetentionItemOut])
async def retention_items(
    run_id: uuid.UUID, _: RetentionManage, db: TenantDBSessionDep, params: Paging
) -> Page[RetentionItemOut]:
    return await privacy.retention.list_items(get_context(), db, run_id, params)


@router.post("/retention/runs/{run_id}/execute", response_model=RetentionRunOut)
async def retention_execute(
    run_id: uuid.UUID, body: RetentionExecuteIn, _: RetentionManage, db: TenantDBSessionDep
) -> RetentionRunOut:
    # The typed confirmation is ``ANONYMIZE`` for inactive-candidate runs and ``EXPIRE`` for class expiry.
    run = await privacy.retention.get_run(get_context(), db, run_id)
    expected = "EXPIRE" if run.kind == "class_expiry" else "ANONYMIZE"
    if body.confirm != expected:
        raise ValidationFailed(f"Type {expected} to confirm", details={"fields": {"confirm": f"Type {expected}"}})
    return await privacy.retention.execute(get_context(), db, run_id)


@router.post("/retention/runs/{run_id}/cancel", response_model=RetentionRunOut)
async def retention_cancel(run_id: uuid.UUID, _: RetentionManage, db: TenantDBSessionDep) -> RetentionRunOut:
    return await privacy.retention.cancel(get_context(), db, run_id)


# ================================================================ support access (tenant side)
@router.get("/support-access", response_model=list[SupportGrantOut])
async def list_support_access(_: SupportApprove, db: TenantDBSessionDep) -> list[SupportGrantOut]:
    return await support_access_service.list_for_tenant(get_context(), db)


@router.post("/support-access/{grant_id}/approve", response_model=SupportGrantOut)
async def approve_support(grant_id: uuid.UUID, _: SupportApprove, db: TenantDBSessionDep) -> SupportGrantOut:
    return await support_access_service.decide(get_context(), db, grant_id, "approved")


@router.post("/support-access/{grant_id}/deny", response_model=SupportGrantOut)
async def deny_support(grant_id: uuid.UUID, _: SupportApprove, db: TenantDBSessionDep) -> SupportGrantOut:
    return await support_access_service.decide(get_context(), db, grant_id, "denied")


@router.post("/support-access/{grant_id}/revoke", response_model=SupportGrantOut)
async def revoke_support(grant_id: uuid.UUID, _: SupportApprove, db: TenantDBSessionDep) -> SupportGrantOut:
    return await support_access_service.decide(get_context(), db, grant_id, "revoked")


# ================================================================ retention classes (Epic 35, AD-34)
@router.get("/retention/classes", response_model=list[RetentionClassOut])
async def retention_classes(
    _: PrivacyManage, db: TenantDBSessionDep, include_archived: bool = False
) -> list[RetentionClassOut]:
    return await privacy.retention_classes.list_classes_v2(get_context(), db, include_archived=include_archived)


@router.post("/retention/classes", response_model=RetentionClassOut, status_code=status.HTTP_201_CREATED)
async def create_retention_class(body: RetentionClassCreate, _: RetentionManage, db: TenantDBSessionDep) -> RetentionClassOut:
    return await privacy.retention_classes.create_class(get_context(), db, body)


@router.patch("/retention/classes/{class_id}", response_model=RetentionClassOut)
async def update_retention_class(
    class_id: uuid.UUID, body: RetentionClassUpdate, _: RetentionManage, db: TenantDBSessionDep
) -> RetentionClassOut:
    return await privacy.retention_classes.update_class(get_context(), db, class_id, body)


@router.post("/retention/classes/{class_id}/archive", response_model=RetentionClassOut)
async def archive_retention_class(class_id: uuid.UUID, _: RetentionManage, db: TenantDBSessionDep) -> RetentionClassOut:
    return await privacy.retention_classes.archive_class(get_context(), db, class_id)


@router.get("/retention/bindings", response_model=list[RetentionBindingOut])
async def retention_bindings(_: PrivacyManage) -> list[RetentionBindingOut]:
    return [RetentionBindingOut(**b) for b in privacy.retention_bindings.bindings_overview()]


@router.post("/retention/expiry/preview", response_model=ClassExpiryPreviewOut, status_code=status.HTTP_201_CREATED)
async def retention_expiry_preview(
    _: RetentionManage, db: TenantDBSessionDep, class_key: Annotated[str, Query(max_length=40)]
) -> ClassExpiryPreviewOut:
    return await privacy.class_expiry.preview(get_context(), db, class_key)


@router.put("/retention/overrides", status_code=status.HTTP_204_NO_CONTENT)
async def put_retention_override(body: RetentionOverrideIn, _: RetentionManage, db: TenantDBSessionDep) -> None:
    await privacy.retention_overrides.set_override(get_context(), db, body)
