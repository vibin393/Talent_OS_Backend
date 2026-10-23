# ruff: file-ignore[line-too-long]
"""Exception lists of a 26AS reconciliation (Epic 51, FR-254), for the latest ready run of LE / FY / quarter.

* *Deducted, not recorded*: statement lines without an active confirmed match and not acknowledged (incl. lines
  whose deductor TAN is not mapped to a Client).
* *Recorded, not deposited*: income-tax TDS deductions of the LE and quarter (not reversed) without an active
  confirmed match (``no_statement_line``), plus confirmed ones whose statement line has booking status ``U``
  (``booking_unmatched``).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.receivables import TdsDeduction, TdsReconMatch, TdsReconRun, TdsStatementLine
from app.schemas.tds_recon import TdsExceptionsOut, TdsRecordedNotDepositedOut
from app.services.receivables.tds_recon import runs
from app.services.receivables.tds_recon.common import fy_short


async def latest_run(
    db: AsyncSession, legal_entity_id: uuid.UUID, fy: str, quarter: str
) -> TdsReconRun | None:
    return (
        await db.execute(
            select(TdsReconRun)
            .where(
                TdsReconRun.legal_entity_id == legal_entity_id,
                TdsReconRun.fy == fy,
                TdsReconRun.quarter == quarter,
                TdsReconRun.status == "ready",
            )
            .order_by(TdsReconRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def quarter_deductions(
    db: AsyncSession, legal_entity_id: uuid.UUID, fy: str, quarter: str
) -> list[TdsDeduction]:
    rows = await db.execute(
        select(TdsDeduction)
        .where(
            TdsDeduction.legal_entity_id == legal_entity_id,
            TdsDeduction.kind == "income_tax",
            TdsDeduction.quarter == f"{fy_short(fy)} {quarter}",
            TdsDeduction.id.not_in(runs.reversed_ids_subquery()),
        )
        .order_by(TdsDeduction.deduction_date, TdsDeduction.id)
    )
    return list(rows.scalars())


async def decided(db: AsyncSession, run_id: uuid.UUID) -> list[tuple[TdsReconMatch, TdsStatementLine]]:
    rows = await db.execute(
        select(TdsReconMatch, TdsStatementLine)
        .join(TdsStatementLine, TdsStatementLine.id == TdsReconMatch.statement_line_id)
        .where(
            TdsStatementLine.run_id == run_id,
            TdsReconMatch.active.is_(True),
            TdsReconMatch.status.in_(("confirmed", "unmatched_ack")),
        )
    )
    return list(rows.tuples())


async def exceptions(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    fy: str,
    quarter: str,
) -> TdsExceptionsOut:
    run = await latest_run(db, legal_entity_id, fy, quarter)
    if run is None:
        return TdsExceptionsOut(run_id=None, deducted_not_recorded=[], recorded_not_deposited=[])
    done = await decided(db, run.id)
    closed_lines = {line.id for _m, line in done}
    confirmed = {
        m.tds_deduction_id: line for m, line in done if m.status == "confirmed" and m.tds_deduction_id
    }
    lines = list(
        (
            await db.execute(
                select(TdsStatementLine)
                .where(TdsStatementLine.run_id == run.id)
                .order_by(TdsStatementLine.line_no)
            )
        ).scalars()
    )
    open_lines = [ln for ln in lines if ln.id not in closed_lines]
    deds = await quarter_deductions(db, legal_entity_id, fy, quarter)
    names = await runs.client_names(db, {d.client_id for d in deds})
    not_deposited: list[TdsRecordedNotDepositedOut] = []
    for d in deds:
        line = confirmed.get(d.id)
        reason = None
        if line is None:
            reason = "no_statement_line"
        elif (line.booking_status or "").upper() == "U":
            reason = "booking_unmatched"
        if reason:
            ref = runs.deduction_ref(d, names)
            not_deposited.append(TdsRecordedNotDepositedOut(**ref.model_dump(), reason=reason))  # type: ignore[arg-type]
    return TdsExceptionsOut(
        run_id=run.id,
        deducted_not_recorded=await runs.lines_out(db, open_lines),
        recorded_not_deposited=not_deposited,
    )
