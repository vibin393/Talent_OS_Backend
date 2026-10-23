# ruff: file-ignore[line-too-long]
"""Per Client and quarter reconciliation status (Epic 51, FR-254): ``reconciled | partially_reconciled |
exceptions | not_started`` with counts and amounts, for one FY (all Legal Entities, or one)."""

import dataclasses
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.legal_entities import LegalEntity
from app.models.receivables import TdsStatementLine
from app.schemas.tds_recon import TdsStatusRowOut
from app.services.receivables.tds_recon import exceptions as exc_mod
from app.services.receivables.tds_recon import runs
from app.services.receivables.tds_recon.common import valid_fy

QUARTERS = ("Q1", "Q2", "Q3", "Q4")


@dataclasses.dataclass
class _Cell:
    has_run: bool = False
    recorded_count: int = 0
    recorded_amount: Decimal = Decimal("0.00")
    matched_count: int = 0
    matched_amount: Decimal = Decimal("0.00")
    statement_count: int = 0
    statement_amount: Decimal = Decimal("0.00")
    open_lines: int = 0


def classify(has_run: bool, recorded: int, matched: int, open_lines: int, statement: int) -> str:
    """Pure status rule: no ready run for the quarter -> ``not_started``; every recorded deduction matched and no
    open statement line -> ``reconciled``; some matched -> ``partially_reconciled``; none -> ``exceptions``."""
    if not has_run:
        return "not_started"
    if matched == recorded and open_lines == 0:
        return "reconciled"
    if matched > 0:
        return "partially_reconciled"
    return "exceptions" if (recorded or statement) else "reconciled"


async def status(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    fy: str,
    legal_entity_id: uuid.UUID | None = None,
) -> list[TdsStatusRowOut]:
    if not valid_fy(fy):
        raise ValidationFailed("Use a financial year like 2026-27", code="invalid_fy")
    stmt = select(LegalEntity.id)
    if legal_entity_id is not None:
        stmt = stmt.where(LegalEntity.id == legal_entity_id)
    entity_ids = list((await db.execute(stmt)).scalars())
    tans = await runs.tan_map(db)
    acc: dict[tuple[uuid.UUID, str], _Cell] = {}

    for le_id in entity_ids:
        for q in QUARTERS:
            run = await exc_mod.latest_run(db, le_id, fy, q)
            deds = await exc_mod.quarter_deductions(db, le_id, fy, q)
            touched: set[uuid.UUID] = set()
            for d in deds:
                c = acc.setdefault((d.client_id, q), _Cell())
                c.recorded_count += 1
                c.recorded_amount += Decimal(d.inr_amount)
                touched.add(d.client_id)
            if run is None:
                continue
            done = await exc_mod.decided(db, run.id)
            closed = {line.id for _m, line in done}
            confirmed = {m.tds_deduction_id for m, _l in done if m.status == "confirmed"}
            for d in deds:
                if d.id in confirmed:
                    c = acc[d.client_id, q]
                    c.matched_count += 1
                    c.matched_amount += Decimal(d.inr_amount)
            lines = list(
                (
                    await db.execute(select(TdsStatementLine).where(TdsStatementLine.run_id == run.id))
                ).scalars()
            )
            for ln in lines:
                client_id = tans.get(ln.deductor_tan)
                if client_id is None:
                    continue
                c = acc.setdefault((client_id, q), _Cell())
                c.statement_count += 1
                c.statement_amount += Decimal(ln.tds_deducted)
                if ln.id not in closed:
                    c.open_lines += 1
                touched.add(client_id)
            for client_id in touched:
                acc[client_id, q].has_run = True
    names = await runs.client_names(db, {cid for cid, _q in acc})
    rows = sorted(acc.items(), key=lambda kv: (names.get(kv[0][0], ""), str(kv[0][0]), kv[0][1]))
    return [
        TdsStatusRowOut(
            client_id=cid,
            client_name=names.get(cid),
            quarter=q,
            status=classify(c.has_run, c.recorded_count, c.matched_count, c.open_lines, c.statement_count),
            recorded_count=c.recorded_count,
            recorded_amount=c.recorded_amount,
            matched_count=c.matched_count,
            matched_amount=c.matched_amount,
            statement_count=c.statement_count,
            statement_amount=c.statement_amount,
        )
        for (cid, q), c in rows
    ]
