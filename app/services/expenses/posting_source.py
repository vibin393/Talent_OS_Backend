"""Pull-only posting source ``expense_line`` (AD-23). The loader returns approved lines of approved /
partially approved claims and never writes; billing / payroll / vendor statements call
``postings.load_unposted`` which excludes items already active in the ledger.

* ``payable`` ledger: reimbursable lines only, ``amount = amount_claim_ccy``.
* ``billing`` ledger: billable lines with a bill amount only, ``amount = bill_amount``.
* ``quantity = 1``, ``unit = 'item'``; payee routing (worker -> payroll, vendor -> statement) is
the consumer's
  job (``query.payee_kind`` filters on the Placement).
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.expenses import ExpenseClaim, ExpenseLine
from app.models.placements import Placement
from app.services.expenses.common import DECIDED, SOURCE_KIND
from app.services.postings import SourceItem, SourceQuery


def _attrs(line: ExpenseLine, claim: ExpenseClaim) -> dict[str, Any]:
    return {
        "category": line.category,
        "billable": bool(line.billable and line.bill_amount is not None),
        "payable": bool(line.reimbursable),
        "reimbursable": bool(line.reimbursable),
        "candidate_id": claim.candidate_id,
        "legal_entity_id": claim.legal_entity_id,
        "vendor_id": claim.vendor_id,
        "approved_at": claim.approved_at,
        "unit": "item",
        "rebill_method": line.rebill_method,
        "markup_pct": line.markup_pct,
        "rebill_tax_mode": line.rebill_tax_mode,
    }


async def load_lines(ctx: RequestContext, db: AsyncSession, query: SourceQuery) -> list[SourceItem]:  # noqa: ARG001
    stmt = (
        select(ExpenseLine, ExpenseClaim)
        .join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
        .join(Placement, Placement.id == ExpenseClaim.placement_id)
        .where(
            ExpenseClaim.status.in_(DECIDED),
            ExpenseLine.line_status == "approved",
            ExpenseLine.expense_date <= query.period_end,
        )
        .order_by(ExpenseLine.expense_date, ExpenseLine.id)
    )
    if query.ledger == "payable":
        stmt = stmt.where(ExpenseLine.reimbursable.is_(True))
    else:
        stmt = stmt.where(ExpenseLine.billable.is_(True), ExpenseLine.bill_amount.is_not(None))
    if query.period_start is not None:
        stmt = stmt.where(ExpenseLine.expense_date >= query.period_start)
    if query.placement_ids is not None:
        stmt = stmt.where(ExpenseClaim.placement_id.in_(list(query.placement_ids)))
    if query.legal_entity_id is not None:
        stmt = stmt.where(ExpenseClaim.legal_entity_id == query.legal_entity_id)
    if query.payee_kind is not None:
        stmt = stmt.where(Placement.payee_kind == query.payee_kind)
    out: list[SourceItem] = []
    for line, claim in (await db.execute(stmt)).all():
        amount = line.amount_claim_ccy if query.ledger == "payable" else line.bill_amount
        out.append(
            SourceItem(
                SOURCE_KIND,
                line.id,
                claim.placement_id,
                line.expense_date,
                Decimal(1),
                Decimal(amount) if amount is not None else None,
                claim.currency,
                _attrs(line, claim),
            )
        )
    return out


def register() -> None:
    from app.services.postings import posting_sources, register_posting_source

    if SOURCE_KIND not in posting_sources():
        register_posting_source(SOURCE_KIND, load_lines, ledgers=frozenset({"billing", "payable"}))
