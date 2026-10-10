# ruff: noqa: E501
"""Worker "My Work" onboarding (Epic 23 A7, P2-10.4, FR-123).

Reads run in the Worker's org-scoped session (``OrgScope.of("worker", candidate_id)``): org RLS limits rows to the
Worker's own case and a RESTRICTIVE policy hides internal tasks. Every task id is re-checked against the
candidate (404 otherwise). Writes that touch internal-only tables (placements approvals, documents, notifications)
then run in an internal tenant session on behalf of the Worker, exactly like the placements portal.
Schemas omit internal tasks, owners, decided-by identity, the template snapshot and compliance source details.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.onboarding import OnboardingCase, OnboardingTask
from app.schemas.onboarding_portal import (
    PortalCaseOut,
    PortalCaseSummary,
    PortalSignatureRefOut,
    PortalTaskOut,
)
from app.services.onboarding import tasks as ops
from app.services.onboarding.common import WORKER_OPEN, case_tasks

_VISIBLE_CASE = ("not_started", "in_progress", "complete")


def _ctx(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, ip: str | None = None, ua: str | None = None
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id, actor_kind="candidate", actor_id=candidate_id, ip=ip, user_agent=ua
    )


async def _own_case(db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID) -> OnboardingCase:
    case = (
        await db.execute(
            select(OnboardingCase).where(
                OnboardingCase.id == case_id,
                OnboardingCase.candidate_id == candidate_id,
                OnboardingCase.initialized_at.is_not(None),
                OnboardingCase.status.in_(_VISIBLE_CASE),
            )
        )
    ).scalar_one_or_none()
    if case is None:
        raise NotFound("Onboarding case not found")
    return case


async def own_task(db: AsyncSession, candidate_id: uuid.UUID, task_id: uuid.UUID) -> OnboardingTask:
    task = (
        await db.execute(
            select(OnboardingTask).where(
                OnboardingTask.id == task_id,
                OnboardingTask.candidate_id == candidate_id,
                OnboardingTask.assignee == "worker",
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise NotFound("Onboarding task not found")
    await _own_case(db, candidate_id, task.case_id)
    return task


def _form(task: OnboardingTask) -> dict[str, Any] | None:
    cfg = task.config or {}
    if task.kind == "client_form":
        return {"fields": cfg.get("fields") or []}
    if task.kind == "tax_details":
        return {"declaration_mode": cfg.get("declaration_mode", "either")}
    if task.kind == "document_upload":
        return {"document_category": cfg.get("document_category"), "aadhaar": bool(cfg.get("aadhaar"))}
    if task.kind == "credential":
        key = cfg.get("credential_type_key")
        return {
            "credential_type_key": key,
            "link": f"/portal/work/credentials?type={key}&onboarding_task={task.id}",
        }
    return None


def _masked(task: OnboardingTask, ident: Any) -> dict[str, str | None]:
    data = task.data or {}
    if task.kind == "bank_details":
        return {
            "bank_last4": data.get("last4") or getattr(ident, "bank_account_last4", None),
            "ifsc": data.get("ifsc"),
        }
    if task.kind == "tax_details":
        return {"pan_last4": data.get("pan_last4") or getattr(ident, "pan_last4", None)}
    if task.kind == "document_upload" and data.get("last4"):
        return {"aadhaar_last4": data.get("last4")}
    return {}


async def _summary(db: AsyncSession, case: OnboardingCase) -> PortalCaseSummary:
    from app.models.placements import Placement

    start = (
        await db.execute(select(Placement.planned_start_date).where(Placement.id == case.placement_id))
    ).scalar_one_or_none()
    open_tasks = sum(
        1 for t in await case_tasks(db, case.id) if t.assignee == "worker" and t.status in WORKER_OPEN
    )
    return PortalCaseSummary(
        id=case.id,
        human_id=case.human_id,
        status=case.status,
        progress_pct=case.progress_pct,
        planned_start_date=start,
        open_tasks=open_tasks,
    )


async def list_cases(db: AsyncSession, candidate_id: uuid.UUID) -> list[PortalCaseSummary]:
    rows = (
        await db.execute(
            select(OnboardingCase)
            .where(
                OnboardingCase.candidate_id == candidate_id,
                OnboardingCase.initialized_at.is_not(None),
                OnboardingCase.status.in_(_VISIBLE_CASE),
            )
            .order_by(OnboardingCase.created_at.desc())
        )
    ).scalars()
    return [await _summary(db, c) for c in list(rows)]


async def get_case(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, case_id: uuid.UUID
) -> PortalCaseOut:
    from app.services import placements_service

    case = await _own_case(db, candidate_id, case_id)
    tasks = [t for t in await case_tasks(db, case.id) if t.assignee == "worker"]
    if case.status != "complete" and any(
        t.kind == "esign" and t.signature_request_id is None and t.status in WORKER_OPEN for t in tasks
    ):  # first portal open creates the agreement
        async with tenant_session(tenant_id) as internal:
            icase = await internal.get(OnboardingCase, case.id)
            for t in await case_tasks(internal, case.id):
                if icase is not None:
                    await ops.prepare_worker_task(_ctx(tenant_id, candidate_id), internal, icase, t)
        tasks = [t for t in await case_tasks(db, case.id) if t.assignee == "worker"]
        for t in tasks:
            await db.refresh(t)
    ident = await placements_service.worker_payment_identity(_ctx(tenant_id, candidate_id), db, candidate_id)
    out: list[PortalTaskOut] = []
    for t in tasks:
        out.append(
            PortalTaskOut(
                id=t.id,
                kind=t.kind,
                label=t.label,
                instructions=t.instructions,
                mandatory=t.mandatory,
                status=t.status,
                due_on=t.due_on,
                return_comment=t.return_comment if t.status == "returned" else None,
                submitted_at=t.submitted_at,
                draft=t.draft if t.status in WORKER_OPEN else None,
                masked=_masked(t, ident),
                form=_form(t),
                signature_request_id=t.signature_request_id,
                signer_id=await ops.worker_signer_id(db, t) if t.kind == "esign" else None,
            )
        )
    summary = await _summary(db, case)
    return PortalCaseOut(**summary.model_dump(), tasks=out)


async def _internal_task(internal: AsyncSession, task_id: uuid.UUID) -> OnboardingTask:
    task = (
        await internal.execute(select(OnboardingTask).where(OnboardingTask.id == task_id).with_for_update())
    ).scalar_one()
    return task


async def save_draft(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    task_id: uuid.UUID,
    draft: Mapping[str, Any],
) -> None:
    await own_task(db, candidate_id, task_id)
    async with tenant_session(tenant_id) as internal:
        await ops.save_draft(
            _ctx(tenant_id, candidate_id), internal, await _internal_task(internal, task_id), draft
        )


async def submit(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: Mapping[str, Any],
) -> PortalTaskOut:
    await own_task(db, candidate_id, task_id)
    async with tenant_session(tenant_id) as internal:
        task = await _internal_task(internal, task_id)
        await ops.submit(_ctx(tenant_id, candidate_id), internal, task, payload)
        status, submitted_at = task.status, task.submitted_at
        masked = _masked(task, None)
    task = await own_task(db, candidate_id, task_id)
    return PortalTaskOut(
        id=task.id,
        kind=task.kind,
        label=task.label,
        instructions=task.instructions,
        mandatory=task.mandatory,
        status=status,
        due_on=task.due_on,
        return_comment=None,
        submitted_at=submitted_at,
        draft=None,
        masked=masked,
        form=_form(task),
    )


async def upload(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    task_id: uuid.UUID,
    data: bytes,
    filename: str | None,
) -> uuid.UUID:
    await own_task(db, candidate_id, task_id)
    async with tenant_session(tenant_id) as internal:
        return await ops.upload(
            _ctx(tenant_id, candidate_id), internal, await _internal_task(internal, task_id), data, filename
        )


async def policy_pdf(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, task_id: uuid.UUID
) -> bytes:
    await own_task(db, candidate_id, task_id)
    async with tenant_session(tenant_id) as internal:
        task = await internal.get(OnboardingTask, task_id)
        assert task is not None
        return await ops.policy_pdf(_ctx(tenant_id, candidate_id), internal, task)


async def acknowledge(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    task_id: uuid.UUID,
    *,
    accepted: bool,
    ip: str | None,
    user_agent: str | None,
) -> None:
    await own_task(db, candidate_id, task_id)
    async with tenant_session(tenant_id) as internal:
        await ops.acknowledge(
            _ctx(tenant_id, candidate_id, ip, user_agent),
            internal,
            await _internal_task(internal, task_id),
            accepted=accepted,
            ip=ip,
            user_agent=user_agent,
        )


async def signature(db: AsyncSession, candidate_id: uuid.UUID, task_id: uuid.UUID) -> PortalSignatureRefOut:
    task = await own_task(db, candidate_id, task_id)
    if task.kind != "esign":
        raise NotFound("Onboarding task not found")
    return PortalSignatureRefOut(
        signature_request_id=task.signature_request_id,
        signer_id=await ops.worker_signer_id(db, task),
        status=task.status,
    )
