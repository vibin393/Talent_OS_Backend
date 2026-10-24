"""Interview endpoints (Epic 7): settings, kits, stage templates, demand plans, interviews, scorecards,
stage outcomes and selection. Each endpoint calls one ``interviews_service`` function (AD-1)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Response, status

from app.core.context import get_context
from app.core.errors import Forbidden
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, current_principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.interviews import (
    ApplyTemplateIn,
    CancelIn,
    InterviewCreate,
    InterviewOut,
    InterviewReschedule,
    InterviewSettingsOut,
    InterviewSettingsUpdate,
    InterviewSummaryOut,
    KitCreate,
    KitOut,
    KitUpdate,
    PlanOut,
    PlanSave,
    ScorecardDraftIn,
    ScorecardListOut,
    ScorecardOut,
    SelectOut,
    StageOutcomeIn,
    StageOutcomeOut,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
)
from app.services import interviews_service as svc

router = APIRouter(prefix="/interviews", tags=["interviews"])
kits_router = APIRouter(prefix="/interview-kits", tags=["interviews"])
templates_router = APIRouter(prefix="/interview-stage-templates", tags=["interviews"])
demand_router = APIRouter(prefix="/demands", tags=["interviews"])
applications_router = APIRouter(prefix="/applications", tags=["interviews"])
me_router = APIRouter(prefix="/me", tags=["interviews"])

Configure = Annotated[Principal, Depends(require("interview:configure"))]
Schedule = Annotated[Principal, Depends(require("interview:schedule"))]
Read = Annotated[Principal, Depends(require("interview:read"))]
OutcomeWrite = Annotated[Principal, Depends(require("interview:outcome"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


async def _schedule_or_configure(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:  # NOSONAR
    if not (principal.has("interview:schedule") or principal.has("interview:configure")):
        raise Forbidden(details={"permission": "interview:configure"})
    return principal


KitReader = Annotated[Principal, Depends(_schedule_or_configure)]


# ================================================================ settings (before /{interview_id})
@router.get("/settings", response_model=InterviewSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.patch("/settings", response_model=InterviewSettingsOut)
async def update_settings(body: InterviewSettingsUpdate, _: Configure, db: DB) -> Any:
    return await svc.update_settings(get_context(), db, body)


# ================================================================ kits
@kits_router.get("", response_model=list[KitOut])
async def list_kits(_: KitReader, db: DB, include_archived: bool = False) -> Any:
    return await svc.list_kits(get_context(), db, include_archived)


@kits_router.post("", response_model=KitOut, status_code=status.HTTP_201_CREATED)
async def create_kit(body: KitCreate, _: Configure, db: DB) -> Any:
    return await svc.create_kit(get_context(), db, body)


@kits_router.get("/{kit_id}", response_model=KitOut)
async def get_kit(kit_id: uuid.UUID, _: KitReader, db: DB) -> Any:
    return await svc.get_kit(get_context(), db, kit_id)


@kits_router.patch("/{kit_id}", response_model=KitOut)
async def update_kit(kit_id: uuid.UUID, body: KitUpdate, _: Configure, db: DB) -> Any:
    return await svc.update_kit(get_context(), db, kit_id, body)


@kits_router.post("/{kit_id}/archive", response_model=KitOut)
async def archive_kit(kit_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await svc.archive_kit(get_context(), db, kit_id)


# ================================================================ stage templates
@templates_router.get("", response_model=list[TemplateOut])
async def list_templates(_: KitReader, db: DB, include_archived: bool = False) -> Any:
    return await svc.list_templates(get_context(), db, include_archived)


@templates_router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateCreate, _: Configure, db: DB) -> Any:
    return await svc.create_template(get_context(), db, body)


@templates_router.get("/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID, _: KitReader, db: DB) -> Any:
    return await svc.get_template(get_context(), db, template_id)


@templates_router.patch("/{template_id}", response_model=TemplateOut)
async def update_template(template_id: uuid.UUID, body: TemplateUpdate, _: Configure, db: DB) -> Any:
    return await svc.update_template(get_context(), db, template_id, body)


# ================================================================ demand plan + interviews
@demand_router.get("/{demand_id}/interview-plan", response_model=PlanOut)
async def get_plan(demand_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    return await svc.get_plan(get_context(), db, demand_id)


@demand_router.put("/{demand_id}/interview-plan", response_model=PlanOut)
async def save_plan(demand_id: uuid.UUID, body: PlanSave, _: Schedule, db: DB) -> Any:
    return await svc.save_plan(get_context(), db, demand_id, body)


@demand_router.post("/{demand_id}/interview-plan/apply-template", response_model=PlanOut)
async def apply_template(demand_id: uuid.UUID, body: ApplyTemplateIn, _: Schedule, db: DB) -> Any:
    return await svc.apply_template(get_context(), db, demand_id, body)


@demand_router.get("/{demand_id}/interviews", response_model=Page[InterviewOut])
async def demand_interviews(
    demand_id: uuid.UUID,
    _: Read,
    db: DB,
    params: Paging,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=100)] = None,
) -> Any:
    return await svc.list_for_demand(get_context(), db, demand_id, params, date_from, date_to, status_filter)


# ================================================================ application-scoped
@applications_router.post(
    "/{application_id}/interviews", response_model=InterviewOut, status_code=status.HTTP_201_CREATED
)
async def schedule_interview(application_id: uuid.UUID, body: InterviewCreate, _: Schedule, db: DB) -> Any:
    return await svc.schedule_interview(get_context(), db, application_id, body)


@applications_router.get("/{application_id}/interviews", response_model=list[InterviewOut])
async def application_interviews(application_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_for_application(get_context(), db, application_id)


@applications_router.post(
    "/{application_id}/stage-outcomes", response_model=StageOutcomeOut, status_code=status.HTTP_201_CREATED
)
async def record_stage_outcome(
    application_id: uuid.UUID, body: StageOutcomeIn, _: OutcomeWrite, db: DB
) -> Any:
    return await svc.record_stage_outcome(get_context(), db, application_id, body)


@applications_router.get("/{application_id}/interview-summary", response_model=InterviewSummaryOut)
async def interview_summary(application_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.interview_summary(get_context(), db, application_id)


@applications_router.post("/{application_id}/select", response_model=SelectOut)
async def select_application(application_id: uuid.UUID, _: OutcomeWrite, db: DB) -> Any:
    return await svc.select_application(get_context(), db, application_id)


# ================================================================ my interviews
@me_router.get("/interviews", response_model=Page[InterviewOut])
async def my_interviews(
    _: CurrentUserDep,
    db: DB,
    params: Paging,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=100)] = None,
) -> Any:
    return await svc.list_mine(get_context(), db, params, date_from, date_to, status_filter)


# ================================================================ interview-scoped
@router.get("/{interview_id}", response_model=InterviewOut)
async def get_interview(interview_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.get_interview(get_context(), db, interview_id)


@router.patch("/{interview_id}/reschedule", response_model=InterviewOut)
async def reschedule_interview(
    interview_id: uuid.UUID, body: InterviewReschedule, _: Schedule, db: DB
) -> Any:
    return await svc.reschedule_interview(get_context(), db, interview_id, body)


@router.post("/{interview_id}/cancel", response_model=InterviewOut)
async def cancel_interview(interview_id: uuid.UUID, body: CancelIn, _: Schedule, db: DB) -> Any:
    return await svc.cancel_interview(get_context(), db, interview_id, body)


@router.post("/{interview_id}/no-show", response_model=InterviewOut)
async def mark_no_show(interview_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    return await svc.mark_no_show(get_context(), db, interview_id)


@router.post("/{interview_id}/complete", response_model=InterviewOut)
async def complete_interview(interview_id: uuid.UUID, _: Schedule, db: DB) -> Any:
    return await svc.complete_interview(get_context(), db, interview_id)


@router.get("/{interview_id}/ics", response_class=Response)
async def download_ics(
    interview_id: uuid.UUID, _: CurrentUserDep, db: DB, participant_id: uuid.UUID | None = None
) -> Response:
    content = await svc.ics_for(get_context(), db, interview_id, participant_id)
    return Response(
        content=content,
        media_type="text/calendar; charset=UTF-8",
        headers={"Content-Disposition": 'attachment; filename="invite.ics"', "Cache-Control": "no-store"},
    )


@router.get("/{interview_id}/scorecards/mine", response_model=ScorecardOut)
async def get_my_scorecard(interview_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.get_my_scorecard(get_context(), db, interview_id)


@router.put("/{interview_id}/scorecards/mine", response_model=ScorecardOut)
async def save_my_scorecard(
    interview_id: uuid.UUID, body: ScorecardDraftIn, _: CurrentUserDep, db: DB
) -> Any:
    return await svc.save_my_scorecard(get_context(), db, interview_id, body)


@router.post("/{interview_id}/scorecards/mine/submit", response_model=ScorecardOut)
async def submit_my_scorecard(
    interview_id: uuid.UUID,
    _: CurrentUserDep,
    db: DB,
    body: Annotated[ScorecardDraftIn | None, Body()] = None,
) -> Any:
    return await svc.submit_my_scorecard(get_context(), db, interview_id, body)


@router.get("/{interview_id}/scorecards", response_model=ScorecardListOut)
async def list_scorecards(interview_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.list_scorecards(get_context(), db, interview_id)
