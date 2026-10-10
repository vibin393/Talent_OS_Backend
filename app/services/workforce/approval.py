# ruff: noqa: E501
"""Plan submission and approval (Epic 44, FR-223, PRD 5.6) -- approval subject ``workforce_plan`` (AD-24) ★.

* ``submit``: ``budgeted -> pending_approval`` (initial) or ``revision_draft -> pending_approval`` (revision). Every
  line needs a budget; a revision needs at least one changed line and is checked against committed slots
  (headcount below Demands / Placements -> 409 ``headcount_below_committed``; a removed line holding a Demand or
  Placement -> 409 ``line_has_committed_slots``). The request uses the tenant's active ``workforce_plan`` chain
  (facts ``total_budget``, ``total_headcount``, ``business_unit_id``, ``client_id``, ``revision``,
  ``changed_lines`` -- chains may branch on the amount); without a chain, or when every chain step is skipped, the
  default step is the Tenant Admin role. A plan is never auto-approved.
* ``on_final_decided``: approval is human only (``require_human``; the engine refuses AI / workflow / system /
  API-key actors). On approval of revision ``r`` the changed lines become effective (``approved_revision = r``) and
  slots follow: ``added`` -> ``headcount`` slots; ``modified`` headcount up -> more slots, down -> cancel open
  unconverted slots; ``removed`` -> cancel its open slots; other field changes carry over. ``PlanLineApproved`` is
  emitted for each newly approved line (dispatched after commit). Rejection returns an initial plan to
  ``rejected`` and a revision to ``revision_draft``.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.events.workforce import PlanLineApproved, WorkforcePlanDecided, WorkforcePlanSubmitted
from app.models.workforce import WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import PlanOut
from app.services.workforce import slots as slots_mod
from app.services.workforce.common import (
    APPROVAL_SUBJECT,
    WRITE,
    conflict,
    get_plan,
    invalid,
    lines_of,
    now,
    user_id,
)
from app.services.workforce.plans import invalid_transition, plan_out
from app.services.workforce.revisions import diff_lines, ensure_revision_row, revision_row, total_budget

DEFAULT_APPROVER_ROLE = "tenant_admin"


def _default_steps() -> list[Any]:
    from app.services.approvals import StepSpec

    return [StepSpec("Workforce plan approval", "role", {"role_key": DEFAULT_APPROVER_ROLE})]


async def _has_chain(db: AsyncSession) -> bool:
    from app.models.approvals import ApprovalChain

    return (
        await db.execute(
            select(ApprovalChain.id).where(
                ApprovalChain.subject_type == APPROVAL_SUBJECT, ApprovalChain.is_active.is_(True)
            )
        )
    ).first() is not None


async def _previous(db: AsyncSession, line: WorkforcePlanLine) -> WorkforcePlanLine | None:
    return await db.get(WorkforcePlanLine, line.supersedes_line_id) if line.supersedes_line_id else None


async def check_revision(db: AsyncSession, plan: WorkforcePlan, lines: list[WorkforcePlanLine]) -> None:  # noqa: ARG001
    """409 when a revision would cut below committed slots (Demands / Placements)."""
    for ln in lines:
        if ln.change_kind not in ("modified", "removed"):
            continue
        lineage = ln.lineage_id or ln.id
        slots = await slots_mod.lineage_slots(db, lineage)
        held = slots_mod.committed(slots)
        if ln.change_kind == "removed":
            if held:
                raise conflict(
                    "line_has_committed_slots",
                    f"'{ln.role_title}' has slots with Demands or Placements and cannot be removed",
                    line_id=str(ln.id),
                    committed=len(held),
                )
            continue
        previous = await _previous(db, ln)
        if previous is None or ln.headcount >= previous.headcount:
            continue
        active = [s for s in slots if s.status != "cancelled"]
        to_cancel = len(active) - ln.headcount
        if to_cancel > len(slots_mod.free_slots(slots)):
            raise conflict(
                "headcount_below_committed",
                f"'{ln.role_title}' already has {len(held)} slots with Demands or Placements",
                line_id=str(ln.id),
                committed=len(held),
                headcount=ln.headcount,
            )


def _facts(plan: WorkforcePlan, lines: list[WorkforcePlanLine], changed: int) -> dict[str, Any]:
    effective = [ln for ln in lines if ln.change_kind != "removed"]
    return {
        "total_budget": total_budget(effective),
        "total_headcount": sum(ln.headcount for ln in effective),
        "business_unit_id": plan.business_unit_id,
        "client_id": plan.client_id,
        "revision": plan.current_revision,
        "changed_lines": changed,
        "currency": plan.currency,
    }


async def submit(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> PlanOut:
    from app.services import approvals

    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status not in ("budgeted", "revision_draft"):
        raise invalid_transition(plan, "pending_approval")
    lines = await lines_of(db, plan.id, plan.current_revision)
    live = [ln for ln in lines if ln.change_kind != "removed"]
    if not live:
        raise invalid("plan_empty", "A plan needs at least one line")
    missing = [ln for ln in live if ln.budget_amount is None]
    if missing:
        raise invalid(
            "budget_missing",
            "Every line needs a budget before the plan is submitted",
            lines=[{"id": str(ln.id), "role_title": ln.role_title} for ln in missing],
        )
    diff = await diff_lines(db, plan.id, plan.current_revision)
    if plan.status == "revision_draft":
        if not diff:
            raise invalid("revision_unchanged", "The revision changes nothing")
        await check_revision(db, plan, lines)
    revision = await ensure_revision_row(ctx, db, plan)
    revision.diff = [d.model_dump(mode="json") for d in diff]
    facts = _facts(plan, lines, len(diff))
    steps = None if await _has_chain(db) else _default_steps()
    result = await approvals.start(ctx, db, APPROVAL_SUBJECT, plan.id, facts, steps=steps)
    if result.status != "pending":  # a chain whose steps were all skipped: a plan is never auto-approved (★)
        result = await approvals.start(ctx, db, APPROVAL_SUBJECT, plan.id, facts, steps=_default_steps())
    plan.status = "pending_approval"
    plan.approval_request_id = result.request_id
    plan.submitted_at = now()
    revision.status = "pending_approval"
    revision.approval_request_id = result.request_id
    revision.submitted_at = plan.submitted_at
    for ln in live:
        if ln.change_kind != "unchanged" and ln.status == "forecast":
            ln.status = "budgeted"
    await db.flush()
    emit(
        db,
        WorkforcePlanSubmitted(
            tenant_id=plan.tenant_id,
            workforce_plan_id=plan.id,
            revision_no=plan.current_revision,
            approval_request_id=result.request_id,
            actor_user_id=user_id(ctx),
        ),
    )
    audit.record(
        "workforce.plan_submitted", "workforce_plan", plan.id, session=db, revision=plan.current_revision
    )
    return await plan_out(ctx, db, plan)


# ================================================================ approval subject callbacks
async def _apply(db: AsyncSession, plan: WorkforcePlan) -> list[WorkforcePlanLine]:
    """Make the current revision effective; returns the newly approved lines (for ``PlanLineApproved``)."""
    r = plan.current_revision
    lines = await lines_of(db, plan.id, r)
    initial = plan.approved_revision is None
    if not initial:
        await check_revision(db, plan, lines)
    approved: list[WorkforcePlanLine] = []
    for ln in lines:
        lineage = ln.lineage_id or ln.id
        kind = "added" if initial else ln.change_kind
        if kind == "added":
            await slots_mod.create_slots(db, plan, lineage, ln.headcount)
            ln.status = "approved"
            approved.append(ln)
        elif kind == "removed":
            for s in await slots_mod.lineage_slots(db, lineage, lock=True):
                if s.status == "open" and s.demand_id is None:
                    slots_mod.cancel(s, f"Line removed in revision {r}")
            ln.status = "cancelled"
        elif kind == "modified":
            current = await slots_mod.lineage_slots(db, lineage, lock=True)
            delta = ln.headcount - len([s for s in current if s.status != "cancelled"])
            if delta > 0:
                await slots_mod.create_slots(db, plan, lineage, delta)
            elif delta < 0:
                for s in slots_mod.free_slots(current)[:-delta]:
                    slots_mod.cancel(s, f"Headcount reduced in revision {r}")
            ln.status = "approved"
            approved.append(ln)
        await db.flush()
    plan.approved_revision = r
    plan.status = "approved"
    plan.approved_at = now()
    await db.flush()
    for ln in lines:
        if ln.change_kind != "removed":
            await slots_mod.refresh_line_status(db, plan, ln.lineage_id or ln.id)
    return approved


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:  # noqa: ARG001
    plan = (
        await db.execute(
            select(WorkforcePlan).where(WorkforcePlan.id == decision.subject_id).with_for_update()
        )
    ).scalar_one_or_none()
    if plan is None or plan.status != "pending_approval":
        return
    revision = await revision_row(db, plan.id, plan.current_revision)
    actor = decision.actor
    if decision.outcome in ("approved", "auto_approved"):
        if actor is None:  # never auto-approved (★)
            return
        require_human(actor)
        approved = await _apply(db, plan)
        if revision is not None:
            revision.status = "approved"
            revision.decided_at = now()
        for ln in approved:
            emit(
                db,
                PlanLineApproved(
                    tenant_id=plan.tenant_id,
                    workforce_plan_id=plan.id,
                    plan_line_id=ln.id,
                    client_id=plan.client_id,
                    headcount=ln.headcount,
                ),
            )
        outcome = "approved"
    else:
        plan.status = "rejected" if plan.approved_revision is None else "revision_draft"
        if revision is not None:
            revision.status = "rejected"
            revision.decided_at = now()
        outcome = "rejected" if decision.outcome == "rejected" else "changes_requested"
    await db.flush()
    emit(
        db,
        WorkforcePlanDecided(
            tenant_id=plan.tenant_id,
            workforce_plan_id=plan.id,
            revision_no=plan.current_revision,
            outcome=outcome,
            decided_by=actor.id if actor is not None and actor.kind == "user" else None,
        ),
    )
    audit.record(
        "workforce.plan_decided",
        "workforce_plan",
        plan.id,
        session=db,
        outcome=outcome,
        revision=plan.current_revision,
    )


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:  # noqa: ARG001
    from app.schemas.approvals import SubjectSummary

    rows = list((await db.execute(select(WorkforcePlan).where(WorkforcePlan.id.in_(ids)))).scalars())
    out: dict[uuid.UUID, Any] = {}
    for p in rows:
        lines = await lines_of(db, p.id, p.current_revision, include_removed=False)
        budget = total_budget(lines).quantize(Decimal("0.01"))
        label = f"{budget} {p.currency}" + (f" (revision {p.current_revision})" if p.current_revision else "")
        out[p.id] = SubjectSummary(label=label, title=f"Workforce plan {p.name}")
    return out
