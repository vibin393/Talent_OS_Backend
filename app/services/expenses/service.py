"""Expenses contract (Epic 27): the internal list and the consumer functions other domains call downward.

* ``mark_reimbursed(ctx, db, line_ids, export_version_id, payroll_reference)`` -- Payroll Export
(Epic 28) calls
  it inside its *mark processed* transaction: a claim becomes ``reimbursed`` once every reimbursable approved
  line has a **posted** payable posting (partially processed claims stay approved). Idempotent.
* ``mark_stated(ctx, db, line_ids, vendor_statement_id)`` -- Vendor Payable Statements (Epic 17)
call it inside
  their finalize transaction; same rule, claim -> ``stated``. Idempotent.
* ``final_expense_settlement`` -- offboarding verifier (R18): no claim of the placement is in flight and every
  approved reimbursable line is posted.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.events import emit
from app.models.expenses import ExpenseClaim, ExpenseLine
from app.models.postings import PayablePosting
from app.schemas.expenses import ClaimSummaryOut
from app.services.expenses.claims import summaries, transition_claim
from app.services.expenses.common import DECIDED, IN_FLIGHT, SOURCE_KIND, now
from app.services.expenses.events import ExpenseClaimReimbursed, ExpenseClaimStated

SYSTEM = ActorRef("system", None)


async def list_claims(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    status: str | None = None,
    placement_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    flag: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    unposted: bool = False,
    limit: int = 500,
) -> list[ClaimSummaryOut]:
    stmt = select(ExpenseClaim).order_by(ExpenseClaim.created_at.desc())
    if status:
        stmt = stmt.where(ExpenseClaim.status == status)
    if placement_id:
        stmt = stmt.where(ExpenseClaim.placement_id == placement_id)
    if client_id:
        stmt = stmt.where(ExpenseClaim.client_id == client_id)
    if candidate_id:
        stmt = stmt.where(ExpenseClaim.candidate_id == candidate_id)
    if flag or date_from or date_to:
        sub = select(ExpenseLine.claim_id)
        if flag:
            sub = sub.where(ExpenseLine.flags.any(flag))  # type: ignore[arg-type]
        if date_from:
            sub = sub.where(ExpenseLine.expense_date >= date_from)
        if date_to:
            sub = sub.where(ExpenseLine.expense_date <= date_to)
        stmt = stmt.where(ExpenseClaim.id.in_(sub))
    if unposted:
        stmt = stmt.where(ExpenseClaim.status.in_(DECIDED))
    rows = list((await db.execute(stmt.limit(limit))).scalars())
    out = await summaries(db, rows)
    return [s for s in out if s.unposted_lines > 0] if unposted else out


async def _posted_payable(db: AsyncSession, line_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    if not line_ids:
        return set()
    rows = await db.execute(
        select(PayablePosting.source_id).where(
            PayablePosting.source_kind == SOURCE_KIND,
            PayablePosting.source_id.in_(list(line_ids)),
            PayablePosting.state == "posted",
        )
    )
    return set(rows.scalars())


async def _settle(
    db: AsyncSession, line_ids: Sequence[uuid.UUID], to_status: str, apply: Callable[[ExpenseClaim], None]
) -> list[ExpenseClaim]:
    if not line_ids:
        return []
    claim_ids = set(
        (await db.execute(select(ExpenseLine.claim_id).where(ExpenseLine.id.in_(list(line_ids))))).scalars()
    )
    done: list[ExpenseClaim] = []
    for claim_id in sorted(claim_ids):
        claim = (
            await db.execute(select(ExpenseClaim).where(ExpenseClaim.id == claim_id).with_for_update())
        ).scalar_one()
        if claim.status not in DECIDED:
            continue  # already reimbursed / stated (idempotent replay) or not approved
        due = list(
            (
                await db.execute(
                    select(ExpenseLine.id).where(
                        ExpenseLine.claim_id == claim.id,
                        ExpenseLine.line_status == "approved",
                        ExpenseLine.reimbursable.is_(True),
                    )
                )
            ).scalars()
        )
        if not due or set(due) - await _posted_payable(db, due):
            continue
        transition_claim(claim, to_status, SYSTEM)
        apply(claim)  # bookkeeping in the same UPDATE as the status
        await db.flush()
        done.append(claim)
    return done


async def mark_reimbursed(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    line_ids: Sequence[uuid.UUID],
    export_version_id: uuid.UUID | None,
    payroll_reference: str | None = None,
) -> list[uuid.UUID]:
    """Claims whose reimbursable approved lines are all posted -> ``reimbursed``; returns their ids."""

    def apply(claim: ExpenseClaim) -> None:
        claim.reimbursed_at = now()
        claim.export_version_id = export_version_id
        claim.payroll_reference = (payroll_reference or "")[:200] or None

    claims = await _settle(db, line_ids, "reimbursed", apply)
    for claim in claims:
        emit(
            db,
            ExpenseClaimReimbursed(
                tenant_id=claim.tenant_id,
                claim_id=claim.id,
                placement_id=claim.placement_id,
                export_version_id=export_version_id,
            ),
        )
    await db.flush()
    return [c.id for c in claims]


async def mark_stated(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    line_ids: Sequence[uuid.UUID],
    vendor_statement_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Claims whose reimbursable approved lines are all posted to a statement -> ``stated``."""

    def apply(claim: ExpenseClaim) -> None:
        claim.stated_at = now()
        claim.vendor_statement_id = vendor_statement_id

    claims = await _settle(db, line_ids, "stated", apply)
    for claim in claims:
        emit(
            db,
            ExpenseClaimStated(
                tenant_id=claim.tenant_id,
                claim_id=claim.id,
                placement_id=claim.placement_id,
                vendor_statement_id=vendor_statement_id,
            ),
        )
    await db.flush()
    return [c.id for c in claims]


async def final_expense_settlement(
    db: AsyncSession, placement_id: uuid.UUID, last_working_day: date
) -> object:
    """Offboarding verifier (R18): nothing in flight and every approved reimbursable line posted."""
    from app.services.placements_service import VerifierResult

    in_flight = (
        await db.execute(
            select(func.count())
            .select_from(ExpenseClaim)
            .where(ExpenseClaim.placement_id == placement_id, ExpenseClaim.status.in_(IN_FLIGHT))
        )
    ).scalar_one()
    approved = list(
        (
            await db.execute(
                select(ExpenseLine.id)
                .join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
                .where(
                    ExpenseClaim.placement_id == placement_id,
                    ExpenseClaim.status.in_(DECIDED),
                    ExpenseLine.line_status == "approved",
                    ExpenseLine.reimbursable.is_(True),
                )
            )
        ).scalars()
    )
    unposted = set(approved) - await _posted_payable(db, approved)
    return VerifierResult(
        done=not in_flight and not unposted,
        details={
            "claims_in_flight": int(in_flight),
            "unposted_lines": len(unposted),
            "last_working_day": last_working_day.isoformat(),
        },
    )
