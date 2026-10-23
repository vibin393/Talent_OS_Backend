# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Manager approval of internal applications (Epic 41 A3, PRD 5.4) on the AD-24 engine (subject
``internal_application``, registered from ``hooks.py``; the engine is never edited).

Explicit steps: an optional recruiter screen (the Demand owner, ``screen_before_manager``) then the **manager**:
Worker -> the current Placement's Account Manager; User -> a lead (``team_members.is_lead``) of any team the User
belongs to (never the User); none -> the users holding ``fallback_approver_role_key``. Unresolvable -> 409
``manager_unresolved``. The engine notifies the manager only when the manager step activates.

A rejection needs a reason of at least 5 characters (422); the Application then moves to ``rejected`` with reason
code ``internal_manager_rejected`` and the Employee sees the reason. Approval releases the pipeline guard.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.models.mobility import MobilityApplication, MobilitySettings
from app.services.approvals.registry import FinalDecision, StepDecision, StepSpec
from app.services.mobility.common import (
    MIN_REJECTION_REASON,
    REJECT_REASON_CODE,
    REJECT_REASON_LABEL,
    conflict,
    guard_bypass,
    invalid,
    now,
)
from app.services.mobility.employees import Employee
from app.services.mobility.events import InternalApplicationDecided

SCREEN_STEP = "Recruiter screen"
MANAGER_STEP = "Manager approval"


async def _team_lead(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.identity import TeamMember, User

    mine = select(TeamMember.team_id).where(TeamMember.user_id == user_id)
    rows = (
        await db.execute(
            select(User.id, User.name)
            .join(TeamMember, TeamMember.user_id == User.id)
            .where(
                TeamMember.team_id.in_(mine),
                TeamMember.is_lead.is_(True),
                TeamMember.user_id != user_id,
                User.is_active.is_(True),
            )
            .order_by(User.name, User.id)
        )
    ).all()
    return rows[0][0] if rows else None


async def resolve_steps(
    db: AsyncSession, emp: Employee, demand: Any, settings: MobilitySettings
) -> tuple[list[StepSpec], uuid.UUID | None]:
    """The explicit approval steps and the resolved manager user (None when the fallback role decides)."""
    from app.services import directory_service

    manager: uuid.UUID | None = None
    if emp.kind == "worker":
        am = emp.account_manager_user_id
        if am is not None and await directory_service.active_user_ids(db, [am]):
            manager = am
    elif emp.user_id is not None:
        manager = await _team_lead(db, emp.user_id)
    steps: list[StepSpec] = []
    if settings.screen_before_manager:
        steps.append(StepSpec(SCREEN_STEP, "user", {"user_id": str(demand.owner_id)}))
    if manager is not None:
        steps.append(StepSpec(MANAGER_STEP, "user", {"user_id": str(manager)}))
        return steps, manager
    role_id = await directory_service.role_id_by_key(db, settings.fallback_approver_role_key)
    holders = await directory_service.user_ids_with_role(db, role_id) if role_id else set()
    holders.discard(emp.user_id)  # type: ignore[arg-type]
    if not holders:
        raise conflict(
            "manager_unresolved",
            "No manager could be found to approve this move. Ask your administrator to set up your team lead or account manager.",
        )
    steps.append(StepSpec(MANAGER_STEP, "role", {"role_key": settings.fallback_approver_role_key}))
    return steps, None


def _decided(db: AsyncSession, row: MobilityApplication, outcome: str) -> None:
    emit(
        db,
        InternalApplicationDecided(
            tenant_id=row.tenant_id,
            mobility_application_id=row.id,
            demand_id=row.demand_id,
            employee_kind=row.employee_kind,
            user_id=row.user_id,
            candidate_id=row.candidate_id,
            outcome=outcome,
        ),
    )


async def _reason_code(db: AsyncSession) -> tuple[str, str | None]:
    """The rejection reason code; seeded once per tenant (falls back to ``other`` if a tenant archived it)."""
    from app.models.pipeline import PipelineReasonCode
    from app.services.pipeline import stages

    await stages.ensure_default_stages(db)
    row = (
        await db.execute(
            select(PipelineReasonCode).where(PipelineReasonCode.kind == "reject", PipelineReasonCode.code == REJECT_REASON_CODE)
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            PipelineReasonCode(
                id=uuid.uuid4(), tenant_id=row_tenant(db), kind="reject", code=REJECT_REASON_CODE, label=REJECT_REASON_LABEL
            )
        )
        await db.flush()
        return REJECT_REASON_CODE, None
    if row.archived_at is not None:
        return "other", REJECT_REASON_LABEL
    return REJECT_REASON_CODE, None


def row_tenant(db: AsyncSession) -> uuid.UUID:
    from app.services.mobility.common import tenant_of

    return tenant_of(db)


async def _reject_application(ctx: RequestContext, db: AsyncSession, row: MobilityApplication, reason: str) -> None:
    from app.models.pipeline import Application, PipelineStage
    from app.services.pipeline import stages
    from app.services.pipeline.applications import transition_application

    app = await db.get(Application, row.application_id)
    if app is None:
        return
    stage = await db.get(PipelineStage, app.stage_id)
    if stage is None or stages.is_terminal(stage.key):
        return
    code, prefix = await _reason_code(db)
    note = f"{prefix}: {reason}" if prefix else reason
    with guard_bypass():
        await transition_application(ctx, db, app.id, "rejected", "user", reason_code=code, note=note[:2000])


async def approve(ctx: RequestContext, db: AsyncSession, row: MobilityApplication) -> None:
    del ctx
    row.status = "approved"
    row.decided_at = now()
    await db.flush()
    audit.record("mobility.application_approved", "mobility_application", row.id, session=db)
    _decided(db, row, "approved")


async def reject(ctx: RequestContext, db: AsyncSession, row: MobilityApplication, reason: str) -> None:
    row.status = "manager_rejected"
    row.rejection_reason = reason
    row.decided_at = now()
    await db.flush()
    await _reject_application(ctx, db, row, reason)
    audit.record("mobility.application_rejected", "mobility_application", row.id, session=db)
    _decided(db, row, "rejected")


# ---------------------------------------------------------------- approval subject callbacks
async def on_step_decided(ctx: RequestContext, db: AsyncSession, decision: StepDecision) -> None:
    del ctx
    if decision.outcome != "approved":
        if len((decision.comment or "").strip()) < MIN_REJECTION_REASON:
            raise invalid(
                "reason_required",
                f"Give the employee a reason of at least {MIN_REJECTION_REASON} characters",
                min_length=MIN_REJECTION_REASON,
            )
        return
    if not decision.is_final:  # the screen passed: the manager step is now active
        row = await db.get(MobilityApplication, decision.subject_id, with_for_update=True)
        if row is not None and row.status == "applied":
            row.status = "manager_approval"
            await db.flush()


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: FinalDecision) -> None:
    row = await db.get(MobilityApplication, decision.subject_id, with_for_update=True)
    if row is None or row.status not in ("applied", "manager_approval"):
        return
    if decision.outcome in ("approved", "auto_approved"):
        await approve(ctx, db, row)
    else:
        await reject(ctx, db, row, (decision.comment or "").strip() or "Not approved")


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    from app.models.candidates import Candidate
    from app.models.demands import Demand
    from app.schemas.approvals import SubjectSummary

    del ctx
    rows = (
        await db.execute(
            select(MobilityApplication.id, Candidate.full_name, Demand.title)
            .join(Candidate, Candidate.id == MobilityApplication.candidate_id)
            .join(Demand, Demand.id == MobilityApplication.demand_id)
            .where(MobilityApplication.id.in_(ids))
        )
    ).all()
    return {rid: SubjectSummary(label=name, title=f"Internal move: {title}") for rid, name, title in rows}
