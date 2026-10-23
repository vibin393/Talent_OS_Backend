"""Expense claims (FR-138, §5.11): creation, line CRUD, submit, recall, reopen and the read projections.
``transition_claim`` is the only writer of ``expense_claims.status`` (AD-4).

Transition table ``EXPENSE_TRANSITIONS`` (§5.11; ★ = human-only): ``draft -> submitted`` (Worker /
User proxy),
``submitted -> in_approval`` (non-final step), ``submitted|in_approval ->
approved|partially_approved|rejected``
★, ``rejected -> draft`` (Worker edits), ``approved|partially_approved -> reimbursed`` (system, payroll
Processed), ``approved|partially_approved -> stated`` (system, vendor statement). Documented extensions:
``submitted -> draft`` (Worker recall before any step decided) and ``approved|partially_approved -> draft`` ★
(reopen by ``expense:admin`` while no line has an active posting).
"""

import dataclasses
import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx, human_only_action, is_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, ValidationFailed
from app.core.events import emit
from app.core.permissions import COMPENSATION_VIEW
from app.core.sequences import next_human_id
from app.events.expenses import ExpenseClaimSubmitted
from app.models.approvals import ApprovalDecision, ApprovalRequest
from app.models.expenses import ExpenseClaim, ExpenseLine, ExpenseLineDecision
from app.models.placements import Placement
from app.models.postings import BillingPosting, PayablePosting
from app.schemas.expenses import (
    ApprovalStepOut,
    ClaimDetailOut,
    ClaimLineOut,
    ClaimSummaryOut,
    LineDecisionOut,
    LineIn,
    LinePreviewOut,
)
from app.services.expenses import amounts
from app.services.expenses.common import (
    DECIDED,
    EDITABLE,
    IN_FLIGHT,
    SOURCE_KIND,
    claim_lines,
    get_placement,
    invalid,
    not_editable,
    now,
)
from app.services.expenses.events import ExpenseClaimRecalled, ExpenseClaimReopened
from app.services.expenses.lines import compute, evaluate_flags, needs_justification, placement_window
from app.services.expenses.policies import rule_for

CLAIMABLE_STATUSES = ("active", "offboarding", "ended")
CLAIM_WINDOW_DAYS = 60


# ================================================================ transitions (AD-4)
@dataclasses.dataclass(frozen=True, slots=True)
class TransitionDef:
    human_only: bool  # ★ rows
    system_only: bool
    note: str


EXPENSE_TRANSITIONS: dict[tuple[str, str], TransitionDef] = {
    ("draft", "submitted"): TransitionDef(False, False, "submit (Worker or User proxy)"),
    ("submitted", "in_approval"): TransitionDef(False, False, "non-final step approved"),
    ("submitted", "approved"): TransitionDef(True, False, "final step approved"),
    ("in_approval", "approved"): TransitionDef(True, False, "final step approved"),
    ("submitted", "partially_approved"): TransitionDef(True, False, "final step, some lines rejected"),
    ("in_approval", "partially_approved"): TransitionDef(True, False, "final step, some lines rejected"),
    ("submitted", "rejected"): TransitionDef(True, False, "rejected by an approver"),
    ("in_approval", "rejected"): TransitionDef(True, False, "rejected by an approver"),
    ("rejected", "draft"): TransitionDef(False, False, "Worker edits a rejected claim"),
    ("approved", "reimbursed"): TransitionDef(False, True, "payroll export Processed"),
    ("partially_approved", "reimbursed"): TransitionDef(False, True, "payroll export Processed"),
    ("approved", "stated"): TransitionDef(False, True, "vendor payable statement finalized"),
    ("partially_approved", "stated"): TransitionDef(False, True, "vendor payable statement finalized"),
    # documented extensions
    ("submitted", "draft"): TransitionDef(False, False, "Worker recall before any step decided"),
    ("approved", "draft"): TransitionDef(True, False, "reopen (no active posting)"),
    ("partially_approved", "draft"): TransitionDef(True, False, "reopen (no active posting)"),
}


def transition_not_allowed(from_status: str, to_status: str) -> ConflictError:
    return ConflictError(
        "This expense claim cannot move to that status",
        code="transition_not_allowed",
        details={"from": from_status, "to": to_status},
    )


def transition_claim(claim: ExpenseClaim, to_status: str, actor: ActorRef, *, auto: bool = False) -> None:
    """Write ``status`` -- the single writer of ``expense_claims.status``."""
    definition = EXPENSE_TRANSITIONS.get((claim.status, to_status))
    if definition is None:
        raise transition_not_allowed(claim.status, to_status)
    if definition.system_only and actor.kind != "system":
        raise Forbidden("Only the system can perform this transition", code="system_only_action")
    if (
        definition.human_only
        and not is_human(actor)
        and not (auto and to_status in ("approved", "partially_approved"))
    ):
        raise human_only_action()
    claim.status = to_status
    claim.lock_version = (claim.lock_version or 0) + 1


# ================================================================ creation
async def _new_human_id(db: AsyncSession) -> str:
    return await next_human_id(db, "expense_claim", "EXP", width=6)


def claimable(placement: Placement) -> bool:
    if placement.status in ("active", "offboarding"):
        return True
    if placement.status == "ended":
        from datetime import timedelta

        from app.services.expenses.common import today

        return placement.end_date is not None and placement.end_date >= today() - timedelta(
            days=CLAIM_WINDOW_DAYS
        )
    return False


async def create_claim(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    placement_id: uuid.UUID,
    *,
    title: str | None = None,
    proxy_reason: str | None = None,
    candidate_id: uuid.UUID | None = None,
) -> ExpenseClaim:
    placement = await get_placement(db, placement_id)
    if candidate_id is not None and placement.candidate_id != candidate_id:
        from app.core.errors import NotFound

        raise NotFound("Placement not found")
    if not claimable(placement):
        raise ConflictError(
            "Expenses can be claimed only on an active placement or within 60 days of its end",
            code="placement_not_claimable",
        )
    amounts.check_currency(placement.currency)
    claim = ExpenseClaim(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        human_id=await _new_human_id(db),
        placement_id=placement.id,
        candidate_id=placement.candidate_id,
        client_id=placement.client_id,
        vendor_id=placement.vendor_id,
        legal_entity_id=placement.legal_entity_id,
        currency=placement.currency,
        title=(title or "").strip() or f"Expenses {now().strftime('%b %Y')}",
        status="draft",
        total_claimed=Decimal("0.00"),
        total_approved=Decimal("0.00"),
        is_proxy=proxy_reason is not None,
        proxy_reason=proxy_reason,
    )
    db.add(claim)
    await db.flush()
    return claim


async def recompute_totals(db: AsyncSession, claim: ExpenseClaim) -> None:
    lines = await claim_lines(db, claim.id)
    claim.total_claimed = amounts.total([ln.amount_claim_ccy for ln in lines])
    claim.total_approved = amounts.total(
        [
            ln.amount_claim_ccy
            for ln in lines
            if ln.line_status == "approved" and claim.status in (*DECIDED, "reimbursed", "stated")
        ]
    )
    await db.flush()


async def _ensure_editable(db: AsyncSession, claim: ExpenseClaim, actor: ActorRef) -> None:
    if claim.status == "rejected":
        transition_claim(claim, "draft", actor)
        claim.rejection_comment = None
        await db.flush()
        for ln in await claim_lines(db, claim.id):
            ln.line_status = "pending"
            ln.bill_amount = None
        await db.flush()
    if claim.status != "draft":
        raise not_editable(claim.status)


# ================================================================ lines
async def preview_line(
    ctx: RequestContext, db: AsyncSession, claim: ExpenseClaim, data: LineIn, line_id: uuid.UUID | None = None
) -> LinePreviewOut:
    placement = await get_placement(db, claim.placement_id)
    c = await compute(ctx, db, claim, placement, data)
    flags = await evaluate_flags(
        ctx,
        db,
        claim,
        category=data.category,
        expense_date=data.expense_date,
        amount_claim_ccy=c.amount_claim_ccy,
        receipt_file_id=data.receipt_file_id,
        receipt_sha256=c.receipt_sha256,
        rule=c.rule,
        line_id=line_id,
    )
    return LinePreviewOut(
        amount=c.amount,
        currency=c.currency,
        km=c.km,
        fx_rate=c.fx_rate,
        fx_rate_date=c.fx_rate_date,
        amount_claim_ccy=c.amount_claim_ccy,
        claim_currency=claim.currency,
        billable=c.billable,
        flags=flags,
        receipt_required=amounts.receipt_required(c.amount_claim_ccy, c.rule.receipt_required_above),
        justification_required=needs_justification(flags),
    )


async def upsert_line(
    ctx: RequestContext,
    db: AsyncSession,
    claim: ExpenseClaim,
    data: LineIn,
    line_id: uuid.UUID | None = None,
    actor: ActorRef | None = None,
) -> ExpenseLine:
    actor = actor or actor_from_ctx(ctx)
    await _ensure_editable(db, claim, actor)
    placement = await get_placement(db, claim.placement_id)
    c = await compute(ctx, db, claim, placement, data)
    line: ExpenseLine | None = None
    if line_id is not None:
        line = (
            await db.execute(
                select(ExpenseLine).where(ExpenseLine.id == line_id, ExpenseLine.claim_id == claim.id)
            )
        ).scalar_one_or_none()
        if line is None:
            from app.core.errors import NotFound

            raise NotFound("Expense line not found")
    flags = await evaluate_flags(
        ctx,
        db,
        claim,
        category=data.category,
        expense_date=data.expense_date,
        amount_claim_ccy=c.amount_claim_ccy,
        receipt_file_id=data.receipt_file_id,
        receipt_sha256=c.receipt_sha256,
        rule=c.rule,
        line_id=line_id,
    )
    values = {
        "expense_date": data.expense_date,
        "category": data.category,
        "description": data.description,
        "amount": c.amount,
        "currency": c.currency,
        "km": c.km,
        "fx_rate": c.fx_rate,
        "fx_source": c.fx_source,
        "fx_rate_date": c.fx_rate_date,
        "amount_claim_ccy": c.amount_claim_ccy,
        "receipt_file_id": data.receipt_file_id,
        "receipt_sha256": c.receipt_sha256,
        "billable": c.billable,
        "reimbursable": data.reimbursable,
        "policy_version_id": c.rule.policy_version_id,
        "flags": flags,
        "justification": data.justification,
        "line_status": "pending",
        "decision_comment": None,
        "bill_amount": None,
    }
    if line is None:
        line = ExpenseLine(id=uuid.uuid4(), tenant_id=claim.tenant_id, claim_id=claim.id, **values)
        db.add(line)
    else:
        for k, v in values.items():
            setattr(line, k, v)
    await db.flush()
    await recompute_totals(db, claim)
    return line


async def delete_line(
    ctx: RequestContext,
    db: AsyncSession,
    claim: ExpenseClaim,
    line_id: uuid.UUID,
    actor: ActorRef | None = None,
) -> None:
    await _ensure_editable(db, claim, actor or actor_from_ctx(ctx))
    line = (
        await db.execute(
            select(ExpenseLine).where(ExpenseLine.id == line_id, ExpenseLine.claim_id == claim.id)
        )
    ).scalar_one_or_none()
    if line is None:
        from app.core.errors import NotFound

        raise NotFound("Expense line not found")
    await db.delete(line)
    await db.flush()
    await recompute_totals(db, claim)


# ================================================================ submit / recall / reopen
async def submit(
    ctx: RequestContext,
    db: AsyncSession,
    claim: ExpenseClaim,
    *,
    actor: ActorRef | None = None,
    proxy_reason: str | None = None,
) -> ExpenseClaim:
    from app.services.expenses import approval

    actor = actor or actor_from_ctx(ctx)
    await _ensure_editable(db, claim, actor)
    lines = await claim_lines(db, claim.id)
    if not lines:
        raise invalid("claim_empty", "Add at least one expense before submitting")
    disabled: list[str] = []
    unjustified: list[str] = []
    for ln in lines:
        rule = await rule_for(db, claim.client_id, ln.category, ln.expense_date)
        flags = await evaluate_flags(
            ctx,
            db,
            claim,
            category=ln.category,
            expense_date=ln.expense_date,
            amount_claim_ccy=ln.amount_claim_ccy,
            receipt_file_id=ln.receipt_file_id,
            receipt_sha256=ln.receipt_sha256,
            rule=rule,
            line_id=ln.id,
        )
        ln.flags = flags
        ln.policy_version_id = rule.policy_version_id
        ln.rebill_method = rule.rebill_method
        ln.markup_pct = rule.markup_pct if rule.rebill_method == "markup" else None
        ln.rebill_tax_mode = rule.rebill_tax_mode
        if rule.rebill_method == "not_billable":
            ln.billable = False
        ln.line_status = "pending"
        ln.decision_comment = None
        ln.bill_amount = None
        if "category_disabled" in flags:
            disabled.append(str(ln.id))
        elif needs_justification(flags) and not (ln.justification or "").strip():
            unjustified.append(str(ln.id))
    await db.flush()
    if disabled:
        raise invalid(
            "category_disabled", "This expense category is not allowed by the policy", line_ids=disabled
        )
    if unjustified:
        raise invalid(
            "justification_required", "Explain the flagged expenses before submitting", line_ids=unjustified
        )
    transition_claim(claim, "submitted", actor)
    claim.submitted_at = now()
    claim.submitted_by_kind = actor.kind
    claim.submitted_by_id = actor.id
    claim.rejection_comment = None
    if proxy_reason:
        claim.is_proxy = True
        claim.proxy_reason = proxy_reason
    await db.flush()
    await recompute_totals(db, claim)
    emit(
        db,
        ExpenseClaimSubmitted(tenant_id=claim.tenant_id, claim_id=claim.id, placement_id=claim.placement_id),
    )
    await approval.start_route(ctx, db, claim, lines, actor)
    return claim


async def _decided_count(db: AsyncSession, request_id: uuid.UUID | None) -> int:
    if request_id is None:
        return 0
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ApprovalDecision)
                .where(
                    ApprovalDecision.request_id == request_id,
                    ApprovalDecision.outcome.in_(("approved", "rejected", "changes_requested")),
                )
            )
        ).scalar_one()
    )


async def recall(
    ctx: RequestContext, db: AsyncSession, claim: ExpenseClaim, actor: ActorRef | None = None
) -> ExpenseClaim:
    from app.services import approvals

    actor = actor or actor_from_ctx(ctx)
    if claim.status != "submitted" or await _decided_count(db, claim.approval_request_id):
        raise transition_not_allowed(claim.status, "draft")
    await approvals.cancel_open(ctx, db, "expense_claim", claim.id)
    transition_claim(claim, "draft", actor)
    claim.submitted_at = None
    await db.flush()
    emit(
        db,
        ExpenseClaimRecalled(tenant_id=claim.tenant_id, claim_id=claim.id, placement_id=claim.placement_id),
    )
    return claim


async def active_posting_ids(db: AsyncSession, line_ids: list[uuid.UUID]) -> dict[uuid.UUID, set[str]]:
    """Ledgers holding a non-released posting, per line (AD-21: locking derives from the ledgers)."""
    out: dict[uuid.UUID, set[str]] = defaultdict(set)
    if not line_ids:
        return out
    for ledger, model in (("payable", PayablePosting), ("billing", BillingPosting)):
        rows = await db.execute(
            select(model.source_id).where(
                model.source_kind == SOURCE_KIND, model.source_id.in_(line_ids), model.state != "released"
            )
        )
        for sid in rows.scalars():
            out[sid].add(ledger)
    return out


async def reopen(ctx: RequestContext, db: AsyncSession, claim: ExpenseClaim, reason: str) -> ExpenseClaim:
    actor = actor_from_ctx(ctx)
    if claim.status not in DECIDED:
        raise ConflictError(
            "Only an approved claim without postings can be reopened",
            code="claim_locked",
            details={"status": claim.status},
        )
    lines = await claim_lines(db, claim.id)
    if await active_posting_ids(db, [ln.id for ln in lines]):
        raise ConflictError("A line of this claim is already posted", code="claim_locked")
    transition_claim(claim, "draft", actor)
    claim.reopen_reason = reason
    claim.total_approved = Decimal("0.00")
    claim.approved_at = None
    claim.submitted_at = None
    await db.flush()
    for ln in lines:
        ln.line_status = "pending"
        ln.bill_amount = None
    await db.flush()
    emit(
        db,
        ExpenseClaimReopened(tenant_id=claim.tenant_id, claim_id=claim.id, placement_id=claim.placement_id),
    )
    return claim


# ================================================================ projections
async def _names(
    db: AsyncSession, claims: list[ExpenseClaim]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    from app.models.candidates import Candidate
    from app.models.clients import Client

    cand_ids = {c.candidate_id for c in claims}
    client_ids = {c.client_id for c in claims}
    workers = (
        {
            r[0]: r[1]
            for r in (
                await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cand_ids)))
            ).all()
        }
        if cand_ids
        else {}
    )
    clients = (
        {
            r[0]: r[1]
            for r in (await db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids)))).all()
        }
        if client_ids
        else {}
    )
    return workers, clients


async def summaries(db: AsyncSession, claims: list[ExpenseClaim]) -> list[ClaimSummaryOut]:
    if not claims:
        return []
    workers, clients = await _names(db, claims)
    by_claim: dict[uuid.UUID, list[ExpenseLine]] = defaultdict(list)
    for ln in (
        await db.execute(select(ExpenseLine).where(ExpenseLine.claim_id.in_([c.id for c in claims])))
    ).scalars():
        by_claim[ln.claim_id].append(ln)
    all_ids = [ln.id for lines in by_claim.values() for ln in lines]
    posted = await active_posting_ids(db, all_ids)
    out: list[ClaimSummaryOut] = []
    for c in claims:
        lines = by_claim[c.id]
        flags = sorted({f for ln in lines for f in (ln.flags or [])})
        unposted = 0
        if c.status in DECIDED:
            for ln in lines:
                if ln.line_status != "approved":
                    continue
                need = set()
                if ln.reimbursable:
                    need.add("payable")
                if ln.billable and ln.bill_amount is not None:
                    need.add("billing")
                if need - posted.get(ln.id, set()):
                    unposted += 1
        out.append(
            ClaimSummaryOut(
                id=c.id,
                human_id=c.human_id,
                placement_id=c.placement_id,
                candidate_id=c.candidate_id,
                worker_name=workers.get(c.candidate_id),
                client_id=c.client_id,
                client_name=clients.get(c.client_id),
                legal_entity_id=c.legal_entity_id,
                currency=c.currency,
                title=c.title,
                status=c.status,
                submitted_at=c.submitted_at,
                total_claimed=c.total_claimed,
                total_approved=c.total_approved,
                line_count=len(lines),
                flags=flags,
                is_proxy=c.is_proxy,
                unposted_lines=unposted,
            )
        )
    return out


async def receipt_names(db: AsyncSession, lines: list[ExpenseLine]) -> dict[uuid.UUID, str]:
    from app.models.files import StoredFile

    ids = {ln.receipt_file_id for ln in lines if ln.receipt_file_id}
    if not ids:
        return {}
    rows = await db.execute(select(StoredFile.id, StoredFile.original_name).where(StoredFile.id.in_(ids)))
    return {r[0]: r[1] for r in rows.all()}


async def current_request(db: AsyncSession, claim: ExpenseClaim) -> ApprovalRequest | None:
    if claim.approval_request_id is None:
        return None
    return (
        await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == claim.approval_request_id))
    ).scalar_one_or_none()


async def detail(ctx: RequestContext, db: AsyncSession, claim: ExpenseClaim) -> ClaimDetailOut:
    summary = (await summaries(db, [claim]))[0]
    lines = await claim_lines(db, claim.id)
    names = await receipt_names(db, lines)
    posted = await active_posting_ids(db, [ln.id for ln in lines])
    principal = ctx.principal
    see_bill = bool(principal is not None and hasattr(principal, "has") and principal.has(COMPENSATION_VIEW))
    decisions: dict[uuid.UUID, list[LineDecisionOut]] = defaultdict(list)
    for d in (
        await db.execute(
            select(ExpenseLineDecision)
            .where(ExpenseLineDecision.claim_id == claim.id)
            .order_by(ExpenseLineDecision.decided_at, ExpenseLineDecision.id)
        )
    ).scalars():
        decisions[d.line_id].append(
            LineDecisionOut(
                step=d.step,
                decision=d.decision,
                comment=d.comment,
                actor_kind=d.actor_kind,
                decided_at=d.decided_at,
            )
        )
    request = await current_request(db, claim)
    current_step = None
    if request is not None and request.status == "pending" and request.current_position is not None:
        step = next(
            (s for s in request.chain_snapshot if int(s["position"]) == request.current_position), None
        )
        current_step = str(step["name"]) if step else None
    history: list[ApprovalStepOut] = []
    if claim.approval_request_id is not None:
        for dec in (
            await db.execute(
                select(ApprovalDecision)
                .where(ApprovalDecision.request_id == claim.approval_request_id)
                .order_by(ApprovalDecision.decided_at, ApprovalDecision.position)
            )
        ).scalars():
            history.append(
                ApprovalStepOut(
                    name=dec.step_name,
                    outcome=dec.outcome,
                    actor_kind=dec.actor_kind,
                    comment=dec.comment,
                    decided_at=dec.decided_at,
                )
            )
    locked = bool(posted)
    return ClaimDetailOut(
        **summary.model_dump(),
        proxy_reason=claim.proxy_reason,
        rejection_comment=claim.rejection_comment,
        approval_request_id=claim.approval_request_id,
        current_step=current_step,
        approved_at=claim.approved_at,
        reimbursed_at=claim.reimbursed_at,
        payroll_reference=claim.payroll_reference,
        stated_at=claim.stated_at,
        reopen_reason=claim.reopen_reason,
        locked=locked,
        can_reopen=claim.status in DECIDED and not locked,
        lines=[
            ClaimLineOut(
                id=ln.id,
                expense_date=ln.expense_date,
                category=ln.category,
                description=ln.description,
                amount=ln.amount,
                currency=ln.currency,
                km=ln.km,
                fx_rate=ln.fx_rate,
                fx_source=ln.fx_source,
                fx_rate_date=ln.fx_rate_date,
                amount_claim_ccy=ln.amount_claim_ccy,
                receipt_file_id=ln.receipt_file_id,
                receipt_name=names.get(ln.receipt_file_id) if ln.receipt_file_id else None,
                billable=ln.billable,
                reimbursable=ln.reimbursable,
                flags=list(ln.flags or []),
                justification=ln.justification,
                line_status=ln.line_status,
                decision_comment=ln.decision_comment,
                rebill_method=ln.rebill_method,
                markup_pct=ln.markup_pct if see_bill else None,
                rebill_tax_mode=ln.rebill_tax_mode,
                bill_amount=ln.bill_amount if see_bill else None,
                payable_posted="payable" in posted.get(ln.id, set()),
                billing_posted="billing" in posted.get(ln.id, set()),
                decisions=decisions.get(ln.id, []),
            )
            for ln in lines
        ],
        approval_history=history,
    )


def editable(claim: ExpenseClaim) -> bool:
    return claim.status in EDITABLE


async def can_recall(db: AsyncSession, claim: ExpenseClaim) -> bool:
    return claim.status == "submitted" and not await _decided_count(db, claim.approval_request_id)


__all__ = [
    "EXPENSE_TRANSITIONS",
    "IN_FLIGHT",
    "ValidationFailed",
    "claimable",
    "create_claim",
    "delete_line",
    "detail",
    "placement_window",
    "preview_line",
    "recall",
    "reopen",
    "submit",
    "summaries",
    "transition_claim",
    "upsert_line",
]
