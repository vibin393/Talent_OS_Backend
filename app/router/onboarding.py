# ruff: noqa: E501
"""Digital onboarding (Epic 23, FR-122..FR-124).

Permissions: ``onboarding:configure`` (templates, settings, match preview), ``onboarding:read`` (board, unmatched,
case detail), ``onboarding:manage`` (start, invite, approve, return, bulk approve, redaction, re-issue signature),
``onboarding:waive`` (★ human-only waive). External principals never reach these routes. One service call each.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.onboarding import (
    ApproveIn,
    BoardRow,
    BulkApproveIn,
    BulkResultOut,
    CaseCreateIn,
    CaseOut,
    InviteOut,
    OnboardingSettingsOut,
    OnboardingSettingsUpdate,
    ReturnIn,
    TaskOut,
    TemplateIn,
    TemplateMatchOut,
    TemplateOut,
    TemplatePatch,
    TemplateTaskIn,
    TemplateTasksPut,
    UnmatchedPlacement,
    WaiveIn,
)
from app.services.onboarding import cases, review, templates

router = APIRouter(prefix="/onboarding", tags=["onboarding"])
cases_router = APIRouter(prefix="/onboarding-cases", tags=["onboarding"])
tasks_router = APIRouter(prefix="/onboarding-tasks", tags=["onboarding"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("onboarding:read"))]
Manage = Annotated[Principal, Depends(require("onboarding:manage"))]
Waive = Annotated[Principal, Depends(require("onboarding:waive"))]
Configure = Annotated[Principal, Depends(require("onboarding:configure"))]


# ---------------------------------------------------------------- settings / templates
@router.get("/settings", response_model=OnboardingSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await templates.get_settings(get_context(), db)


@router.patch("/settings", response_model=OnboardingSettingsOut)
async def update_settings(data: OnboardingSettingsUpdate, _: Configure, db: DB) -> Any:
    return await templates.update_settings(get_context(), db, data)


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(_: Configure, db: DB, include_archived: bool = False) -> Any:
    return await templates.list_templates(get_context(), db, include_archived=include_archived)


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(data: TemplateIn, _: Configure, db: DB) -> Any:
    return await templates.create_template(get_context(), db, data)


@router.get("/templates/match", response_model=TemplateMatchOut)
async def preview_match(placement_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await templates.preview_match(get_context(), db, placement_id)


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await templates.get_template(get_context(), db, template_id)


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def patch_template(template_id: uuid.UUID, data: TemplatePatch, _: Configure, db: DB) -> Any:
    return await templates.patch_template(get_context(), db, template_id, data)


@router.put("/templates/{template_id}/tasks", response_model=TemplateOut)
async def put_tasks(template_id: uuid.UUID, data: TemplateTasksPut, _: Configure, db: DB) -> Any:
    return await templates.put_tasks(get_context(), db, template_id, data)


@router.post(
    "/templates/{template_id}/tasks", response_model=TemplateOut, status_code=status.HTTP_201_CREATED
)
async def add_task(template_id: uuid.UUID, data: TemplateTaskIn, _: Configure, db: DB) -> Any:
    return await templates.add_task(get_context(), db, template_id, data)


@router.delete("/templates/{template_id}/tasks/{task_id}", response_model=TemplateOut)
async def delete_task(template_id: uuid.UUID, task_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await templates.delete_task(get_context(), db, template_id, task_id)


# ---------------------------------------------------------------- cases
@cases_router.get("", response_model=list[BoardRow])
async def board(
    _: Read,
    db: DB,
    starts_within: Annotated[int | None, Query(ge=0, le=365)] = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    owner: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
) -> Any:
    return await cases.board(
        get_context(),
        db,
        starts_within=starts_within,
        status=status_,
        owner_id=owner,
        placement_id=placement_id,
    )


@cases_router.get("/unmatched", response_model=list[UnmatchedPlacement])
async def unmatched(_: Read, db: DB) -> Any:
    return await cases.unmatched(get_context(), db)


@cases_router.post("", response_model=CaseOut, status_code=status.HTTP_201_CREATED)
async def start_case(data: CaseCreateIn, _: Manage, db: DB) -> Any:
    return await cases.start_for_placement(get_context(), db, data.placement_id)


@cases_router.get("/by-placement/{placement_id}", response_model=CaseOut)
async def case_for_placement(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.case_for_placement(get_context(), db, placement_id)


@cases_router.get("/{case_id}", response_model=CaseOut)
async def get_case(case_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await cases.case_detail(get_context(), db, case_id)


@cases_router.post("/{case_id}/invite", response_model=InviteOut)
async def invite(case_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await cases.invite_by_id(get_context(), db, case_id)


@cases_router.post("/{case_id}/approve", response_model=BulkResultOut)
async def bulk_approve(case_id: uuid.UUID, data: BulkApproveIn, _: Manage, db: DB) -> Any:
    return await review.bulk_approve(get_context(), db, case_id, data.task_ids)


# ---------------------------------------------------------------- tasks
@tasks_router.post("/{task_id}/approve", response_model=TaskOut)
async def approve(task_id: uuid.UUID, data: ApproveIn, _: Manage, db: DB) -> Any:
    return await review.approve(get_context(), db, task_id, data)


@tasks_router.post("/{task_id}/return", response_model=TaskOut)
async def return_task(task_id: uuid.UUID, data: ReturnIn, _: Manage, db: DB) -> Any:
    return await review.return_task(get_context(), db, task_id, data)


@tasks_router.post("/{task_id}/waive", response_model=TaskOut)
async def waive(task_id: uuid.UUID, data: WaiveIn, _: Waive, db: DB) -> Any:
    return await review.waive(get_context(), db, task_id, data)


@tasks_router.post("/{task_id}/confirm-redaction", response_model=TaskOut)
async def confirm_redaction(task_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await review.confirm_redaction(get_context(), db, task_id)


@tasks_router.post("/{task_id}/reissue-signature", response_model=TaskOut)
async def reissue_signature(task_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await review.reissue_signature(get_context(), db, task_id)
