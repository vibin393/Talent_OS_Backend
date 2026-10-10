"""Demand endpoints (Epic 4): ``/api/v1/demands`` (+ JD, posting, intake, assignments, approvals history)
and the demand settings under ``/api/v1/settings``. Each endpoint calls one service function."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.ai import require_ai_feature
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.approvals import RequestOut
from app.schemas.demands import (
    AssignmentsIn,
    AssignmentsOut,
    BiasCheckOut,
    BiasedTermIn,
    BiasedTermOut,
    BusinessCalendarIn,
    BusinessCalendarOut,
    ConfirmAiFieldsIn,
    DemandCreate,
    DemandDocumentOut,
    DemandFilters,
    DemandOut,
    DemandSettingsIn,
    DemandSettingsOut,
    DemandUpdate,
    JdGenerateIn,
    JdVersionIn,
    JdVersionOut,
    PostingIn,
    PostingOut,
    ReasonCodesOut,
    SlaTargetOut,
    SlaTargetsIn,
    TransitionIn,
)
from app.services import demands as svc

router = APIRouter(prefix="/demands", tags=["demands"])
settings_router = APIRouter(prefix="/settings", tags=["demand-settings"])

Read = Annotated[Principal, Depends(require("demand:read"))]
Create = Annotated[Principal, Depends(require("demand:create"))]
Update = Annotated[Principal, Depends(require("demand:update"))]
Delete = Annotated[Principal, Depends(require("demand:delete"))]
Assign = Annotated[Principal, Depends(require("demand:assign"))]
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


# ================================================================ collection
@router.get("", response_model=Page[DemandOut])
async def list_demands(_: Read, db: DB, filters: Annotated[DemandFilters, Query()], params: Paging) -> Any:
    return await svc.records.list_demands(get_context(), db, filters, params)


@router.post("", response_model=DemandOut, status_code=status.HTTP_201_CREATED)
async def create_demand(body: DemandCreate, _: Create, db: DB) -> Any:
    return await svc.records.create_demand(get_context(), db, body)


@router.get("/reason-codes", response_model=ReasonCodesOut)
async def reason_codes(_: Read) -> Any:
    return svc.reason_codes()


@router.post(
    "/intake",
    response_model=DemandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ai_feature("demand_intake"))],  # Epic 12 (FR-62)
)
async def intake(
    _: Create,
    db: DB,
    client_id: Annotated[uuid.UUID, Form()],
    text: Annotated[str | None, Form(max_length=200_000)] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    return await svc.intake.intake(get_context(), db, client_id=client_id, text=text, upload=file)


# ================================================================ one demand
@router.get("/{demand_id}", response_model=DemandOut)
async def get_demand(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.records.get_demand(get_context(), db, demand_id)


@router.patch("/{demand_id}", response_model=DemandOut)
async def update_demand(demand_id: uuid.UUID, body: DemandUpdate, _: Update, db: DB) -> Any:
    return await svc.records.update_demand(get_context(), db, demand_id, body)


@router.post("/{demand_id}/confirm-ai-fields", response_model=DemandOut)
async def confirm_ai_fields(demand_id: uuid.UUID, body: ConfirmAiFieldsIn, _: Update, db: DB) -> Any:
    return await svc.records.confirm_ai_fields(get_context(), db, demand_id, body.fields)


@router.post("/{demand_id}/archive", response_model=DemandOut)
async def archive_demand(demand_id: uuid.UUID, _: Delete, db: DB) -> Any:
    return await svc.records.archive_demand(get_context(), db, demand_id)


@router.post("/{demand_id}/submit", response_model=DemandOut)
async def submit_demand(demand_id: uuid.UUID, _: Update, db: DB) -> Any:
    return await svc.submit(get_context(), db, demand_id)


@router.post("/{demand_id}/transition", response_model=DemandOut)
async def transition_demand(demand_id: uuid.UUID, body: TransitionIn, _: Update, db: DB) -> Any:
    return await svc.transition(get_context(), db, demand_id, body.to, body.reason_code, body.comment)


@router.get("/{demand_id}/approvals", response_model=list[RequestOut])
async def approval_history(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.approval_history(get_context(), db, demand_id)


@router.get("/{demand_id}/documents", response_model=list[DemandDocumentOut])
async def list_documents(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.settings.list_documents(get_context(), db, demand_id)


# ================================================================ JD versions / posting
@router.get("/{demand_id}/jd-versions", response_model=list[JdVersionOut])
async def list_jd_versions(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.jd.list_jd_versions(get_context(), db, demand_id)


@router.post("/{demand_id}/jd-versions", response_model=JdVersionOut, status_code=status.HTTP_201_CREATED)
async def save_jd_version(demand_id: uuid.UUID, body: JdVersionIn, _: Update, db: DB) -> Any:
    return await svc.jd.save_jd_version(get_context(), db, demand_id, body)


@router.post(
    "/{demand_id}/jd-versions/generate",
    response_model=JdVersionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ai_feature("jd_generation"))],  # Epic 12 (FR-62)
)
async def generate_jd(demand_id: uuid.UUID, body: JdGenerateIn, _: Update, db: DB) -> Any:
    return await svc.jd.generate_jd(get_context(), db, demand_id, body)


@router.post(
    "/{demand_id}/jd-versions/{version_id}/restore",
    response_model=JdVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def restore_jd_version(demand_id: uuid.UUID, version_id: uuid.UUID, _: Update, db: DB) -> Any:
    return await svc.jd.restore_jd_version(get_context(), db, demand_id, version_id)


@router.post("/{demand_id}/jd-versions/{version_id}/bias-check", response_model=BiasCheckOut)
async def bias_check(demand_id: uuid.UUID, version_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.jd.bias_check(get_context(), db, demand_id, version_id)


@router.get("/{demand_id}/posting", response_model=PostingOut | None)
async def get_posting(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.jd.get_posting(get_context(), db, demand_id)


@router.post("/{demand_id}/posting", response_model=PostingOut)
async def publish(demand_id: uuid.UUID, body: PostingIn, _: Update, db: DB) -> Any:
    return await svc.jd.publish(get_context(), db, demand_id, body)


@router.delete("/{demand_id}/posting", response_model=PostingOut)
async def unpublish(demand_id: uuid.UUID, _: Update, db: DB) -> Any:
    return await svc.jd.unpublish(get_context(), db, demand_id)


# ================================================================ assignments
@router.get("/{demand_id}/assignments", response_model=AssignmentsOut)
async def list_assignments(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.assignment.list_assignments(get_context(), db, demand_id)


@router.put("/{demand_id}/assignments", response_model=AssignmentsOut)
async def set_assignments(demand_id: uuid.UUID, body: AssignmentsIn, _: Assign, db: DB) -> Any:
    return await svc.assignment.set_assignments(get_context(), db, demand_id, body)


# ================================================================ settings (settings:manage)
@settings_router.get("/biased-terms", response_model=list[BiasedTermOut])
async def list_biased_terms(_: SettingsManage, db: DB) -> Any:
    return await svc.jd.list_biased_terms(get_context(), db)


@settings_router.post("/biased-terms", response_model=BiasedTermOut)
async def upsert_biased_term(body: BiasedTermIn, _: SettingsManage, db: DB) -> Any:
    return await svc.jd.upsert_biased_term(get_context(), db, body)


@settings_router.delete("/biased-terms/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_biased_term(term_id: uuid.UUID, _: SettingsManage, db: DB) -> Response:
    await svc.jd.delete_biased_term(get_context(), db, term_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@settings_router.get("/sla-targets", response_model=list[SlaTargetOut])
async def list_sla_targets(_: SettingsManage, db: DB) -> Any:
    return await svc.settings.list_sla_targets(get_context(), db)


@settings_router.put("/sla-targets", response_model=list[SlaTargetOut])
async def replace_sla_targets(body: SlaTargetsIn, _: SettingsManage, db: DB) -> Any:
    return await svc.settings.replace_sla_targets(get_context(), db, body)


@settings_router.get("/business-calendar", response_model=BusinessCalendarOut)
async def get_calendar(_: SettingsManage, db: DB) -> Any:
    return await svc.settings.get_calendar(get_context(), db)


@settings_router.put("/business-calendar", response_model=BusinessCalendarOut)
async def put_calendar(body: BusinessCalendarIn, _: SettingsManage, db: DB) -> Any:
    return await svc.settings.put_calendar(get_context(), db, body)


@settings_router.get("/demands", response_model=DemandSettingsOut)
async def get_demand_settings(_: CurrentUserDep, db: DB) -> Any:
    return await svc.settings.get_demand_settings(get_context(), db)


@settings_router.put("/demands", response_model=DemandSettingsOut)
async def put_demand_settings(body: DemandSettingsIn, _: SettingsManage, db: DB) -> Any:
    return await svc.settings.put_demand_settings(get_context(), db, body)
