"""Demand lifecycle (FR-11, FR-12, FR-65; stories 4.1/4.2).

``transition_demand`` is the **only** writer of ``demands.status`` (AD-4): it validates against
``transitions.TRANSITIONS``, writes, adds a timeline entry and emits ``DemandStatusChanged``. Entering Open
sets ``opened_at`` (first time), emits ``DemandOpened`` and starts the SLA clocks; leaving Open unpublishes
the posting (actor system).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, ValidationFailed
from app.core.events import emit
from app.events.demands import DemandOpened, DemandStatusChanged, DemandSubmitted
from app.models.demands import Demand, DemandSkill
from app.schemas.approvals import SubjectSummary
from app.services import approvals
from app.services.demands import jd, sla
from app.services.demands.common import UPDATE, actor_kind_of, get_for, now, timeline
from app.services.demands.constants import REASON_CODES
from app.services.demands.transitions import (
    ENDPOINT_REFUSED_OPEN_SOURCES,
    ENDPOINT_REFUSED_TARGETS,
    HUMAN_ONLY_REJECTED,
    REASON_REQUIRED,
    TRANSITIONS,
    ActorKind,
)

REQUIRED_FOR_SUBMIT: tuple[str, ...] = (
    "title",
    "client_id",
    "positions",
    "employment_type",
    "country",
    "work_mode",
)


def human_only() -> Forbidden:
    return Forbidden("Only a person can change the demand status", code="human_only_action")


def invalid_transition(from_status: str, to_status: str) -> ConflictError:
    return ConflictError(
        f"A demand cannot move from {from_status} to {to_status}",
        code="invalid_transition",
        details={"from": from_status, "to": to_status},
    )


async def transition_demand(
    ctx: RequestContext,
    db: AsyncSession,
    demand: Demand,
    to: str,
    actor_kind: ActorKind | str,
    reason_code: str | None = None,
    comment: str | None = None,
    *,
    offer_withdrawn: bool = False,
) -> Demand:
    if actor_kind in HUMAN_ONLY_REJECTED:
        raise human_only()
    from_status = demand.status
    allowed = TRANSITIONS.get((from_status, to))  # type: ignore[arg-type]
    if allowed is None:
        raise invalid_transition(from_status, to)
    if actor_kind not in allowed:
        raise human_only() if "user" in allowed else invalid_transition(from_status, to)
    if to in REASON_REQUIRED:
        if not reason_code:
            raise ValidationFailed("A reason code is required", code="reason_required")
        if reason_code not in REASON_CODES[to]:
            raise ValidationFailed(
                "Unknown reason code", code="invalid_reason_code", details={"allowed": list(REASON_CODES[to])}
            )
    if (from_status, to) == ("filled", "open") and not (
        demand.positions > demand.positions_filled or offer_withdrawn
    ):
        raise ConflictError(
            "Raise the position count (or withdraw an accepted offer) before reopening", code="reopen_guard"
        )

    demand.status = to
    if to in REASON_REQUIRED or (from_status, to) == ("pending_approval", "draft"):
        demand.status_reason_code = reason_code
        demand.status_comment = comment
    elif to == "open":
        demand.status_reason_code = None
        demand.status_comment = comment
    first_open = to == "open" and demand.opened_at is None
    if first_open:
        demand.opened_at = now()
    await db.flush()
    emit(
        db,
        DemandStatusChanged(
            tenant_id=demand.tenant_id,
            demand_id=demand.id,
            from_status=from_status,
            to_status=to,
            actor_kind=str(actor_kind),
            reason_code=reason_code,
        ),
    )
    await timeline(
        ctx,
        db,
        demand.id,
        f"Status changed from {from_status} to {to}",
        from_status=from_status,
        to_status=to,
        reason_code=reason_code,
        actor_kind=str(actor_kind),
    )
    if to == "open":
        if first_open:
            emit(db, DemandOpened(tenant_id=demand.tenant_id, demand_id=demand.id))
        await sla.start_open_clocks(ctx, db, demand)
    if from_status == "open":
        await jd.unpublish_demand(ctx, db, demand, "system")
    return demand


async def request_transition(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    to: str,
    reason_code: str | None = None,
    comment: str | None = None,
    *,
    actor_kind: ActorKind | None = None,
) -> Demand:
    """``POST /demands/{id}/transition``: user moves only (hold, resume, reopen, cancel, close)."""
    kind = actor_kind or actor_kind_of(ctx)
    if kind in HUMAN_ONLY_REJECTED:
        raise human_only()
    demand = await get_for(ctx, db, demand_id, UPDATE)
    if demand.archived_at is not None:
        raise ConflictError("The demand is archived", code="demand_archived")
    if to in ENDPOINT_REFUSED_TARGETS or (to == "open" and demand.status in ENDPOINT_REFUSED_OPEN_SOURCES):
        raise invalid_transition(demand.status, to)
    return await transition_demand(ctx, db, demand, to, kind, reason_code, comment)


# ================================================================ submit + approvals
def margin_percent(demand: Demand) -> Decimal | None:
    """(bill - pay_max) / bill * 100 when both rates are set (same currency / unit by construction)."""
    if demand.bill_rate is None or demand.pay_rate_max is None or demand.bill_rate == 0:
        return None
    return ((demand.bill_rate - demand.pay_rate_max) / demand.bill_rate * 100).quantize(Decimal("0.01"))


def approval_facts(demand: Demand) -> dict[str, Any]:
    return {
        "bill_rate": demand.bill_rate,
        "pay_rate": demand.pay_rate_max,
        "margin_percent": margin_percent(demand),
        "positions": demand.positions,
        "client_id": str(demand.client_id),
        "country": demand.country,
        "employment_type": demand.employment_type,
        "currency": demand.currency,
        "rate_unit": demand.rate_unit,
    }


async def submit_demand(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, *, actor_kind: ActorKind | None = None
) -> Demand:
    kind = actor_kind or actor_kind_of(ctx)
    if kind in HUMAN_ONLY_REJECTED:
        raise human_only()
    demand = await get_for(ctx, db, demand_id, UPDATE)
    if demand.archived_at is not None:
        raise ConflictError("The demand is archived", code="demand_archived")
    if demand.status != "draft":
        raise invalid_transition(demand.status, "pending_approval")
    missing = [f for f in REQUIRED_FOR_SUBMIT if getattr(demand, f) in (None, "")]
    mandatory = (
        await db.execute(
            select(DemandSkill.id).where(DemandSkill.demand_id == demand.id, DemandSkill.kind == "mandatory")
        )
    ).first()
    if mandatory is None:
        missing.append("mandatory_skills")
    if missing:
        raise ValidationFailed(
            "Complete the required fields before submitting",
            code="incomplete_demand",
            details={"fields": [{"field": f, "message": "required"} for f in missing]},
        )
    if demand.ai_extracted_fields:
        raise ConflictError(
            "Confirm or edit the AI-extracted fields before submitting",
            code="unconfirmed_ai_fields",
            details={"fields": list(demand.ai_extracted_fields)},
        )
    # Epic 16 (FR-91): the bill rate must clear the client's effective rate card (409 rate_exception_*).
    from app.services import rate_gate_port

    await rate_gate_port.check("demand_submit", ctx, db, demand)
    result = await approvals.start(ctx, db, "demand", demand.id, approval_facts(demand))
    auto = result.status == "auto_approved"
    await transition_demand(ctx, db, demand, "open" if auto else "pending_approval", kind)
    emit(
        db,
        DemandSubmitted(
            tenant_id=demand.tenant_id,
            demand_id=demand.id,
            approval_request_id=result.request_id,
            auto_approved=auto,
        ),
    )
    return demand


async def _on_approved(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> None:
    demand = await db.get(Demand, demand_id, with_for_update=True)
    if demand is None or demand.status != "pending_approval":
        return
    await transition_demand(ctx, db, demand, "open", "user")


async def _on_returned(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, outcome: str, comment: str
) -> None:
    demand = await db.get(Demand, demand_id, with_for_update=True)
    if demand is None or demand.status != "pending_approval":
        return
    await transition_demand(ctx, db, demand, "draft", "user", reason_code=outcome, comment=comment)


async def _describe(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, SubjectSummary]:
    rows = await db.execute(select(Demand.id, Demand.req_id, Demand.title).where(Demand.id.in_(ids)))
    return {i: SubjectSummary(label=r, title=t) for i, r, t in rows.all()}


def register() -> None:
    approvals.register_subject("demand", _on_approved, _on_returned, _describe)
