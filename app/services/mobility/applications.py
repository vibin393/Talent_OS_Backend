# ruff: file-ignore[line-too-long]
"""Internal applications (Epic 41 A3, FR-210): a P1 Application with ``source='internal'`` plus a
``mobility_applications`` overlay (link = ``application_id`` UNIQUE + ``applications.source_ref``).

* ``apply`` -- opening open + eligible (409 ``not_eligible`` with reasons), disclosure acknowledged (422
  ``disclosure_required``), approval steps resolved first (409 ``manager_unresolved``), then
  ``pipeline_service.create_application`` and ``approvals.start`` with explicit steps.
* ``pipeline_guard`` -- registered with P1 ``register_transition_guard``: an Application whose overlay is
  ``applied | manager_approval | manager_rejected | withdrawn`` cannot leave ``applied`` (409
  ``manager_approval_pending``); mobility's own moves run under ``guard_bypass``.
* ``withdraw`` -- the Employee: approval cancelled, Application -> ``withdrawn``.
* Employee views (``my_applications``) and recruiter views (``list_for_demand``, ``by_application``).

Confidentiality: until approval the application is visible only to users with access to the Demand and to the
Employee; nothing is written on the Employee's current Placement.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.events import emit
from app.models.mobility import HOLD_STATUSES, LIVE_STATUSES, InternalOpening, MobilityApplication
from app.schemas.mobility import ApplyIn, InternalApplicationOut, MyApplicationOut
from app.services.mobility import approval, board, eligibility
from app.services.mobility.common import (
    APPROVAL_SUBJECT,
    MANAGE,
    conflict,
    guard_bypass,
    has,
    invalid,
    not_found,
    now,
    settings_row,
    today,
)
from app.services.mobility.employees import Employee, candidate_for_user
from app.services.mobility.events import InternalApplicationSubmitted

WITHDRAWABLE = ("applied", "manager_approval", "approved")


# ================================================================ pipeline guard (P1 registry)
async def pipeline_guard(ctx: RequestContext, db: AsyncSession, app: Any, from_stage: Any, to_stage: Any, actor: str) -> None:
    from app.services.mobility.common import bypassing

    del ctx, to_stage, actor
    if getattr(app, "source", None) != "internal" or bypassing() or from_stage.key != "applied":
        return
    status = (
        await db.execute(select(MobilityApplication.status).where(MobilityApplication.application_id == app.id))
    ).scalar_one_or_none()
    if status in HOLD_STATUSES:
        raise conflict(
            "manager_approval_pending",
            "This internal application is waiting for the employee's manager to approve it",
            status=status,
        )


# ================================================================ apply
async def apply(
    ctx: RequestContext, db: AsyncSession, emp: Employee, demand_id: uuid.UUID, data: ApplyIn
) -> MobilityApplication:
    from app.models.demands import Demand
    from app.services import approvals
    from app.services.pipeline.applications import create_application

    if not data.disclosure_ack:
        raise invalid("disclosure_required", "Confirm that you have read how your manager will be involved")
    settings = await settings_row(db)
    enabled = settings.board_enabled if emp.kind == "user" else settings.worker_board_enabled
    if not enabled:
        raise not_found("Opening")
    await board.sync_demand(db, demand_id)
    opening = await db.get(InternalOpening, demand_id)
    demand = await db.get(Demand, demand_id)
    if opening is None or opening.status != "open" or demand is None:
        raise not_found("Opening")
    rules = (await eligibility.load_rules(db, [demand_id])).get(demand_id)
    result = eligibility.evaluate(rules, settings.min_tenure_default_months, emp, today())
    if not result.eligible:
        raise conflict("not_eligible", "You are not eligible for this opening", reasons=list(result.reasons))
    live = await _live_for(db, emp, demand_id)
    if live is not None:
        raise conflict("application_exists", "You already applied for this opening", application_id=str(live.id))
    steps, manager = await approval.resolve_steps(db, emp, demand, settings)
    candidate_id = emp.candidate_id if emp.kind == "worker" else await candidate_for_user(ctx, db, emp)
    assert candidate_id is not None
    overlay_id = uuid.uuid4()
    app = await create_application(
        ctx,
        db,
        demand_id,
        candidate_id,
        source="internal",
        source_ref=str(overlay_id),
        actor="user" if emp.kind == "user" else "candidate",
        owner_id=demand.owner_id,
    )
    at = now()
    row = MobilityApplication(
        id=overlay_id,
        tenant_id=emp.tenant_id,
        application_id=app.id,
        demand_id=demand_id,
        candidate_id=candidate_id,
        employee_kind=emp.kind,
        user_id=emp.user_id,
        current_placement_id=emp.placement_id,
        status="applied" if settings.screen_before_manager else "manager_approval",
        manager_user_id=manager,
        cover_note=data.cover_note or None,
        disclosed_at=at,
        submitted_at=at,
    )
    db.add(row)
    await db.flush()
    initiator = ActorRef("user", emp.user_id) if emp.kind == "user" else ActorRef("candidate", candidate_id)
    started = await approvals.start(
        ctx,
        db,
        APPROVAL_SUBJECT,
        row.id,
        {"demand_id": str(demand_id), "employee_kind": emp.kind},
        steps=steps,
        initiated_by=initiator,
    )
    row.approval_request_id = started.request_id
    await db.flush()
    if started.status == "auto_approved" and row.status in ("applied", "manager_approval"):
        await approval.approve(ctx, db, row)
    audit.record("mobility.application_submitted", "mobility_application", row.id, session=db, demand_id=str(demand_id), employee_kind=emp.kind)
    emit(
        db,
        InternalApplicationSubmitted(
            tenant_id=row.tenant_id,
            mobility_application_id=row.id,
            demand_id=demand_id,
            employee_kind=emp.kind,
            user_id=emp.user_id,
            candidate_id=candidate_id,
        ),
    )
    return row


async def _live_for(db: AsyncSession, emp: Employee, demand_id: uuid.UUID) -> MobilityApplication | None:
    apps = await board.own_applications(db, emp)
    row = apps.get(demand_id)
    return row if row is not None and row.status in LIVE_STATUSES else None


# ================================================================ withdraw
async def own_row(db: AsyncSession, emp: Employee, mobility_application_id: uuid.UUID, *, lock: bool = False) -> MobilityApplication:
    row = await db.get(MobilityApplication, mobility_application_id, with_for_update=lock)
    mine = row is not None and (
        (emp.kind == "user" and row.user_id == emp.user_id) or (emp.kind == "worker" and row.employee_kind == "worker" and row.candidate_id == emp.candidate_id)
    )
    if not mine:
        raise not_found()
    assert row is not None
    return row


async def withdraw(ctx: RequestContext, db: AsyncSession, emp: Employee, mobility_application_id: uuid.UUID) -> MobilityApplication:
    from app.models.pipeline import Application, PipelineStage
    from app.services import approvals
    from app.services.pipeline import stages
    from app.services.pipeline.applications import transition_application

    row = await own_row(db, emp, mobility_application_id, lock=True)
    if row.status not in WITHDRAWABLE:
        raise conflict("not_withdrawable", "This application can no longer be withdrawn", status=row.status)
    await approvals.cancel_open(ctx, db, APPROVAL_SUBJECT, row.id)
    row.status = "withdrawn"
    row.decided_at = now()
    await db.flush()
    app = await db.get(Application, row.application_id)
    stage = await db.get(PipelineStage, app.stage_id) if app is not None else None
    if app is not None and stage is not None and not stages.is_terminal(stage.key):
        with guard_bypass():
            await transition_application(
                ctx,
                db,
                app.id,
                "withdrawn",
                "user" if emp.kind == "user" else "candidate",
                reason_code="other",
                note="Internal application withdrawn by the employee",
            )
    audit.record("mobility.application_withdrawn", "mobility_application", row.id, session=db)
    return row


# ================================================================ Employee views
async def _stage_names(db: AsyncSession, app_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
    from app.models.pipeline import Application, PipelineStage

    if not app_ids:
        return {}
    rows = await db.execute(
        select(Application.id, PipelineStage.key, PipelineStage.label)
        .join(PipelineStage, PipelineStage.id == Application.stage_id)
        .where(Application.id.in_(app_ids))
    )
    return {aid: (key, label) for aid, key, label in rows.all()}


async def _titles(db: AsyncSession, demand_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.demands import Demand

    if not demand_ids:
        return {}
    return dict((await db.execute(select(Demand.id, Demand.title).where(Demand.id.in_(demand_ids)))).tuples().all())


def my_out(row: MobilityApplication, title: str, stage: tuple[str, str] | None) -> MyApplicationOut:
    released = row.status in ("approved", "transfer_confirmed")
    return MyApplicationOut(
        id=row.id,
        demand_id=row.demand_id,
        demand_title=title,
        status=row.status,  # type: ignore[arg-type]
        stage_name=stage[1] if released and stage else None,
        rejection_reason=row.rejection_reason if row.status == "manager_rejected" else None,
        submitted_at=row.submitted_at,
        decided_at=row.decided_at,
        can_withdraw=row.status in WITHDRAWABLE,
    )


async def my_views(db: AsyncSession, rows: list[MobilityApplication], titles: dict[uuid.UUID, str] | None = None) -> list[MyApplicationOut]:
    titles = titles if titles is not None else await _titles(db, {r.demand_id for r in rows})
    stages = await _stage_names(db, [r.application_id for r in rows])
    return [my_out(r, titles.get(r.demand_id, "Opening"), stages.get(r.application_id)) for r in rows]


async def user_rows(db: AsyncSession, user_id: uuid.UUID) -> list[MobilityApplication]:
    return list(
        (
            await db.execute(
                select(MobilityApplication)
                .where(MobilityApplication.user_id == user_id)
                .order_by(MobilityApplication.submitted_at.desc())
            )
        ).scalars()
    )


async def worker_rows(db: AsyncSession, candidate_id: uuid.UUID) -> list[MobilityApplication]:
    """Run in the Worker's org-scoped session: another Worker's rows are invisible."""
    return list(
        (
            await db.execute(
                select(MobilityApplication)
                .where(MobilityApplication.candidate_id == candidate_id, MobilityApplication.employee_kind == "worker")
                .order_by(MobilityApplication.submitted_at.desc())
            )
        ).scalars()
    )


# ================================================================ recruiter views
async def _transfers(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    from app.services.mobility import transfers

    return await transfers.transfers_for(db, ids)


async def internal_views(db: AsyncSession, rows: list[MobilityApplication]) -> list[InternalApplicationOut]:
    from app.models.candidates import Candidate
    from app.models.identity import User
    from app.models.pipeline import Application

    if not rows:
        return []
    names = dict(
        (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_({r.candidate_id for r in rows})))).tuples().all()
    )
    mgr_ids = {r.manager_user_id for r in rows if r.manager_user_id}
    managers = dict((await db.execute(select(User.id, User.name).where(User.id.in_(mgr_ids)))).tuples().all()) if mgr_ids else {}
    human = dict(
        (await db.execute(select(Application.id, Application.human_id).where(Application.id.in_({r.application_id for r in rows})))).tuples().all()
    )
    titles = await _titles(db, {r.demand_id for r in rows})
    stages = await _stage_names(db, [r.application_id for r in rows])
    transfers = await _transfers(db, [r.id for r in rows])
    return [
        InternalApplicationOut(
            id=r.id,
            application_id=r.application_id,
            application_human_id=human.get(r.application_id),
            demand_id=r.demand_id,
            demand_title=titles.get(r.demand_id),
            candidate_id=r.candidate_id,
            candidate_name=names.get(r.candidate_id),
            employee_kind=r.employee_kind,  # type: ignore[arg-type]
            user_id=r.user_id,
            current_placement_id=r.current_placement_id,
            status=r.status,  # type: ignore[arg-type]
            stage_key=stages.get(r.application_id, (None, None))[0],
            approval_request_id=r.approval_request_id,
            manager_user_id=r.manager_user_id,
            manager_name=managers.get(r.manager_user_id) if r.manager_user_id else None,
            rejection_reason=r.rejection_reason,
            cover_note=r.cover_note,
            submitted_at=r.submitted_at,
            decided_at=r.decided_at,
            transfer=transfers.get(r.id),
        )
        for r in rows
    ]


async def _demand_visible(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> None:
    """``mobility:manage`` or demand read scope (404 outside the caller's scope either way)."""
    from app.services.demands import matching_port as demands

    await demands.get_demand(ctx, db, demand_id)


async def list_for_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[InternalApplicationOut]:
    await _demand_visible(ctx, db, demand_id)
    rows = list(
        (
            await db.execute(
                select(MobilityApplication)
                .where(MobilityApplication.demand_id == demand_id)
                .order_by(MobilityApplication.submitted_at.desc())
            )
        ).scalars()
    )
    return await internal_views(db, rows)


async def by_application(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> InternalApplicationOut:
    row = (
        await db.execute(select(MobilityApplication).where(MobilityApplication.application_id == application_id))
    ).scalar_one_or_none()
    if row is None:
        raise not_found()
    await _demand_visible(ctx, db, row.demand_id)
    return (await internal_views(db, [row]))[0]


async def visible_row(ctx: RequestContext, db: AsyncSession, mobility_application_id: uuid.UUID, *, lock: bool = False) -> MobilityApplication:
    row = await db.get(MobilityApplication, mobility_application_id, with_for_update=lock)
    if row is None:
        raise not_found()
    await _demand_visible(ctx, db, row.demand_id)
    return row


def may_manage(ctx: RequestContext) -> bool:
    return has(ctx, MANAGE)
