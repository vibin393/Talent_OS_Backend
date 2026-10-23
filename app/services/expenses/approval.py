"""Expense claim approval on the extended approvals engine (FR-139, AD-24).

* ``start_route`` resolves the Placement's ``placement_time_route`` (registered by Timesheets; ``expenses``
  never imports ``time``) with the Client step only when at least one line is billable at submit.
* Per-line decisions arrive as ``StepDecision.payload["line_decisions"]`` (``subject_payload`` pass-through,
  R5): ``[{line_id, decision: approve|reject, comment}]`` -- a rejected line needs a comment (the step comment
  is the fallback); an earlier rejection is final; the Client step sees and decides billable lines only
  (non-billable lines pass through). Decisions are stored in ``expense_line_decisions`` and ``line_status`` is
  updated synchronously in the deciding transaction.
* When no billable line is left pending before a Client step, that step is skipped (``skip_current_step``,
  actor ``system``, recorded); when every line is rejected the remaining steps are skipped too.
* Final: all lines rejected -> ``rejected``; some -> ``partially_approved``; none -> ``approved``
(bill amounts
  computed for approved billable lines). A whole-claim rejection at any step -> ``rejected`` with the comment.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.expenses import ExpenseClaimDecided
from app.models.approvals import ApprovalRequest
from app.models.expenses import ExpenseClaim, ExpenseLine, ExpenseLineDecision
from app.schemas.approvals import DecideIn, SubjectSummary
from app.services import approvals
from app.services.approvals import FinalDecision, StepDecision
from app.services.expenses import amounts
from app.services.expenses.claims import recompute_totals, transition_claim
from app.services.expenses.common import (
    CLIENT_KINDS,
    IN_FLIGHT,
    ROUTE,
    SUBJECT,
    claim_lines,
    get_claim,
    invalid,
    now,
)
from app.services.expenses.events import ExpenseLineDecided


def _facts(claim: ExpenseClaim, lines: list[ExpenseLine]) -> dict[str, Any]:
    billable = [ln for ln in lines if ln.billable]
    return {
        "claim_id": str(claim.id),
        "placement_id": str(claim.placement_id),
        "client_id": str(claim.client_id),
        "currency": claim.currency,
        "total_claimed": str(claim.total_claimed),
        "billable_lines": len(billable),
        "line_count": len(lines),
    }


async def start_route(
    ctx: RequestContext, db: AsyncSession, claim: ExpenseClaim, lines: list[ExpenseLine], actor: ActorRef
) -> None:
    include_client = any(ln.billable for ln in lines)
    steps = await approvals.resolve_route(
        ctx, db, ROUTE, placement_id=claim.placement_id, include_client=include_client
    )
    if not steps:
        raise ValidationFailed("This placement has no approval route", code="approval_route_empty")
    result = await approvals.start(
        ctx, db, SUBJECT, claim.id, _facts(claim, lines), steps=steps, initiated_by=actor
    )
    claim.approval_request_id = result.request_id
    await db.flush()
    if result.status == "auto_approved" and claim.status in IN_FLIGHT:
        await _finalize(db, claim, ActorRef("system", None), auto=True)


def _step_kind(request: ApprovalRequest | None, position: int | None) -> str | None:
    if request is None or position is None:
        return None
    step = next((s for s in request.chain_snapshot if int(s["position"]) == position), None)
    if step is None:
        return None
    kind = step.get("resolved_kind")
    return str(kind) if kind else ("user" if step.get("approver_user_id") else "role")


async def _request(db: AsyncSession, request_id: uuid.UUID) -> ApprovalRequest:
    return (await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))).scalar_one()


def _parse_line_decisions(payload: Any) -> dict[uuid.UUID, tuple[str, str | None]]:
    out: dict[uuid.UUID, tuple[str, str | None]] = {}
    raw = (
        (payload or {}).get("line_decisions")
        if isinstance(payload, dict) or hasattr(payload, "get")
        else None
    )
    for item in raw or []:
        try:
            line_id = uuid.UUID(str(item.get("line_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise invalid("line_unknown", "Unknown expense line") from exc
        decision = str(item.get("decision") or "approve")
        if decision not in ("approve", "reject"):
            raise invalid("line_decision_invalid", "A line decision is approve or reject")
        comment = (str(item.get("comment") or "")).strip() or None
        out[line_id] = (decision, comment)
    return out


def _record(
    db: AsyncSession,
    claim: ExpenseClaim,
    line: ExpenseLine,
    request_id: uuid.UUID | None,
    step: str,
    decision: str,
    comment: str | None,
    actor: ActorRef,
) -> None:
    db.add(
        ExpenseLineDecision(
            id=uuid.uuid4(),
            tenant_id=claim.tenant_id,
            line_id=line.id,
            claim_id=claim.id,
            request_id=request_id,
            step=step[:100],
            decision=decision,
            comment=comment,
            actor_kind=actor.kind,
            actor_id=actor.id,
            decided_at=now(),
        )
    )
    emit(
        db,
        ExpenseLineDecided(
            tenant_id=claim.tenant_id,
            claim_id=claim.id,
            line_id=line.id,
            step=step[:100],
            decision=decision,
            actor_kind=actor.kind,
        ),
    )


def validate_line_decisions(
    lines: list[ExpenseLine],
    decisions: dict[uuid.UUID, tuple[str, str | None]],
    *,
    client_step: bool,
    fallback_comment: str | None,
) -> None:
    by_id = {ln.id: ln for ln in lines}
    missing_comment: list[str] = []
    for line_id, (decision, comment) in decisions.items():
        line = by_id.get(line_id)
        if line is None or (client_step and not line.billable):
            raise invalid("line_unknown", "Unknown expense line", line_id=str(line_id))
        if line.line_status == "rejected" and decision == "approve":
            raise ConflictError(
                "This line was already rejected",
                code="line_already_rejected",
                details={"line_id": str(line_id)},
            )
        if decision == "reject" and not (comment or fallback_comment):
            missing_comment.append(str(line_id))
    if missing_comment:
        raise ValidationFailed(
            "A comment is required for each rejected line",
            code="comment_required",
            details={"line_ids": missing_comment},
        )


async def on_step_decided(ctx: RequestContext, db: AsyncSession, decision: StepDecision) -> None:
    claim = await get_claim(db, decision.subject_id, lock=True)
    if claim.status not in IN_FLIGHT:
        return
    request = await _request(db, decision.request_id)
    client_step = _step_kind(request, decision.position) in CLIENT_KINDS
    lines = await claim_lines(db, claim.id)
    visible = [ln for ln in lines if ln.billable or not client_step]
    decisions = _parse_line_decisions(decision.payload)
    if decision.outcome == "approved":
        validate_line_decisions(lines, decisions, client_step=client_step, fallback_comment=decision.comment)
        for ln in visible:
            if ln.line_status != "pending":
                continue
            choice, comment = decisions.get(ln.id, ("approve", None))
            if choice == "reject":
                ln.line_status = "rejected"
                ln.decision_comment = comment or decision.comment
                _record(
                    db,
                    claim,
                    ln,
                    request.id,
                    decision.step_name,
                    "rejected",
                    ln.decision_comment,
                    decision.actor,
                )
            else:
                _record(db, claim, ln, request.id, decision.step_name, "approved", comment, decision.actor)
        if client_step:
            for ln in lines:
                if not ln.billable and ln.line_status == "pending":
                    _record(db, claim, ln, request.id, decision.step_name, "passed", None, decision.actor)
    else:
        for ln in visible:
            if ln.line_status == "pending":
                ln.line_status = "rejected"
                ln.decision_comment = decision.comment
                _record(
                    db,
                    claim,
                    ln,
                    request.id,
                    decision.step_name,
                    "rejected",
                    decision.comment,
                    decision.actor,
                )
    await db.flush()
    if decision.outcome != "approved":
        return
    # skip steps that have nothing left to decide
    for _ in range(20):
        if request.status != "pending" or request.current_position is None:
            break
        pending = [ln for ln in lines if ln.line_status == "pending"]
        if pending and not (
            _step_kind(request, request.current_position) in CLIENT_KINDS
            and not any(ln.billable for ln in pending)
        ):
            break
        reason = "No billable lines left to decide" if pending else "All expense lines were rejected"
        await approvals.skip_current_step(ctx, db, request.id, reason=reason)
    if request.status == "pending" and claim.status == "submitted":
        transition_claim(claim, "in_approval", decision.actor)
        await db.flush()


async def _finalize(db: AsyncSession, claim: ExpenseClaim, actor: ActorRef, *, auto: bool = False) -> None:
    lines = await claim_lines(db, claim.id)
    rejected = 0
    for ln in lines:
        if ln.line_status == "pending":
            ln.line_status = "approved"
        if ln.line_status == "rejected":
            rejected += 1
            ln.bill_amount = None
            continue
        ln.bill_amount = (
            amounts.bill_amount(ln.amount_claim_ccy, ln.rebill_method, ln.markup_pct, claim.currency)
            if ln.billable
            else None
        )
    if lines and rejected == len(lines):
        outcome = "rejected"
        claim.rejection_comment = claim.rejection_comment or "All expense lines were rejected"
    elif rejected:
        outcome = "partially_approved"
    else:
        outcome = "approved"
    # totals and bookkeeping go in the same UPDATE as the status (the claim is immutable once approved)
    claim.total_claimed = amounts.total([ln.amount_claim_ccy for ln in lines])
    claim.total_approved = amounts.total(
        [ln.amount_claim_ccy for ln in lines if ln.line_status == "approved"]
    )
    if outcome == "rejected":
        claim.total_approved = amounts.total([])
    transition_claim(claim, outcome, actor, auto=auto)
    if outcome != "rejected":
        claim.approved_at = now()
    await db.flush()
    emit(
        db,
        ExpenseClaimDecided(
            tenant_id=claim.tenant_id, claim_id=claim.id, placement_id=claim.placement_id, outcome=outcome
        ),
    )


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: FinalDecision) -> None:  # ruff: ignore[unused-function-argument]
    claim = await get_claim(db, decision.subject_id, lock=True)
    if claim.status not in IN_FLIGHT:
        return
    actor = decision.actor or ActorRef("system", None)
    if decision.outcome in ("approved", "auto_approved"):
        await _finalize(db, claim, actor, auto=decision.actor is None)
        return
    for ln in await claim_lines(db, claim.id):
        if ln.line_status == "pending":
            ln.line_status = "rejected"
            ln.decision_comment = decision.comment
    transition_claim(claim, "rejected", actor)
    claim.rejection_comment = decision.comment
    await db.flush()
    await recompute_totals(db, claim)
    emit(
        db,
        ExpenseClaimDecided(
            tenant_id=claim.tenant_id, claim_id=claim.id, placement_id=claim.placement_id, outcome="rejected"
        ),
    )


async def describe(
    ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, SubjectSummary]:
    del ctx
    from app.models.candidates import Candidate

    rows = (
        await db.execute(
            select(
                ExpenseClaim.id,
                ExpenseClaim.human_id,
                ExpenseClaim.title,
                ExpenseClaim.currency,
                ExpenseClaim.total_claimed,
                Candidate.full_name,
            )
            .join(Candidate, Candidate.id == ExpenseClaim.candidate_id, isouter=True)
            .where(ExpenseClaim.id.in_(ids))
        )
    ).all()
    return {
        r.id: SubjectSummary(
            label=f"Expense claim {r.human_id}",
            title=f"{r.full_name or 'Worker'} · {r.title} · {r.currency} {r.total_claimed}",
        )
        for r in rows
    }


async def decide_as(
    ctx: RequestContext,
    db: AsyncSession,
    claim: ExpenseClaim,
    action: str,
    comment: str | None,
    line_decisions: list[dict[str, Any]],
    actor: ActorRef | None = None,
) -> None:
    """Decide the current step of a claim's request (internal app / Client Portal) with per-line decisions."""
    if claim.approval_request_id is None or claim.status not in IN_FLIGHT:
        raise ConflictError("This expense claim is not waiting for approval", code="request_not_pending")
    request = await _request(db, claim.approval_request_id)
    client_step = _step_kind(request, request.current_position) in CLIENT_KINDS
    if action == "approve":
        validate_line_decisions(
            await claim_lines(db, claim.id),
            _parse_line_decisions({"line_decisions": line_decisions}),
            client_step=client_step,
            fallback_comment=(comment or "").strip() or None,
        )
    await approvals.decide(
        ctx,
        db,
        claim.approval_request_id,
        DecideIn(action=action, comment=comment, subject_payload={"line_decisions": line_decisions}),  # type: ignore[arg-type]
        actor=actor or actor_from_ctx(ctx),  # type: ignore[arg-type]
    )


async def awaiting_client(principal: Any, request: ApprovalRequest | None) -> bool:
    if request is None or request.status != "pending" or request.current_approver_kind not in CLIENT_KINDS:
        return False
    if request.current_approver_kind == "client_user":
        return request.current_approver_client_user_id == getattr(principal, "id", None)
    snap = next((s for s in request.chain_snapshot if int(s["position"]) == request.current_position), {})
    if str((snap.get("org") or [None, None])[1]) != str(getattr(principal, "org_id", None)):
        return False
    return "time:approve" in (getattr(principal, "portal_permissions", None) or ())
