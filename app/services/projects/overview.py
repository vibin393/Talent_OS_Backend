# ruff: file-ignore[line-too-long]
"""Project 360 (Epic 45, FR-231) for the Delivery Manager: header, SOW, roles, assignments, timeline summary,
milestones, deliverables, budgets / burn, invoices and risks.

Risks: overdue deliverables, unfilled roles starting within 14 days, Worker over-allocation, budget >= 80 %, SOW
ending within 30 days. ``risk_counts`` is the list-view variant (grouped queries, without over-allocation).
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.money import MoneyOut
from app.models.billing import CreditNote, CreditNoteLine, Invoice, InvoiceLine
from app.models.candidates import Candidate
from app.models.commercials import Contract
from app.models.projects import Deliverable, Project, ProjectAssignment, ProjectBurn, ProjectRole
from app.schemas.projects import Project360Out, ProjectInvoiceRef, ProjectRisk, SowSummary
from app.services.projects import assignments, budgets, burn, deliverables, projects, roles, timeline
from app.services.projects.common import get_project, today

DONE = ("accepted", "cancelled")
ROLE_SOON_DAYS = 14
SOW_SOON_DAYS = 30
BUDGET_RISK_PCT = Decimal(80)


def _sow_out(c: Contract | None) -> SowSummary | None:
    if c is None:
        return None
    return SowSummary(
        contract_id=c.id,
        human_id=c.human_id,
        title=c.title,
        pricing_model=c.pricing_model,
        total_value=MoneyOut(amount=c.value_cap, currency=c.currency) if c.value_cap is not None else None,
        status=c.status,
        start_date=c.start_date,
        end_date=c.end_date,
    )


async def invoices_for(db: AsyncSession, project: Project) -> list[ProjectInvoiceRef]:
    """Issued invoices (and their issued credit notes) touching the project: lines of its assigned placements or
    lines billing its milestones."""
    placement_ids = set(
        (
            await db.execute(
                select(ProjectAssignment.placement_id).where(ProjectAssignment.project_id == project.id)
            )
        ).scalars()
    )
    milestone_lines = await burn.milestone_line_ids(db, project)
    conds = []
    if placement_ids:
        conds.append(InvoiceLine.placement_id.in_(placement_ids))
    if milestone_lines:
        conds.append(InvoiceLine.id.in_(milestone_lines))
    if not conds:
        return []
    invoice_ids = set(
        (
            await db.execute(
                select(InvoiceLine.invoice_id)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(
                    Invoice.status.in_(("issued", "cancelled")),
                    Invoice.client_id == project.client_id,
                    or_(*conds),
                )
            )
        ).scalars()
    )
    if not invoice_ids:
        return []
    out = [
        ProjectInvoiceRef(
            kind="invoice",
            id=i.id,
            number=i.number,
            doc_date=i.doc_date,
            status=i.status,
            amount=MoneyOut(amount=i.document_total, currency=i.currency),
        )
        for i in (await db.execute(select(Invoice).where(Invoice.id.in_(invoice_ids)))).scalars()
    ]
    notes = (
        await db.execute(
            select(CreditNote).where(
                CreditNote.original_invoice_id.in_(invoice_ids), CreditNote.status == "issued"
            )
        )
    ).scalars()
    out += [
        ProjectInvoiceRef(
            kind="credit_note",
            id=n.id,
            number=n.number,
            doc_date=n.doc_date,
            status=n.status,
            amount=MoneyOut(amount=n.document_total, currency=n.currency),
        )
        for n in notes
    ]
    _ = CreditNoteLine  # credit notes are matched on their original invoice
    return sorted(out, key=lambda r: (r.doc_date or date.min, r.number or ""), reverse=True)


def _risks(
    on: date,
    *,
    deliverable_rows: Sequence[Deliverable],
    role_rows: Sequence[ProjectRole],
    burn_rows: Sequence[ProjectBurn],
    sow: Contract | None,
    over_workers: Sequence[tuple[uuid.UUID, str | None]] = (),
) -> list[ProjectRisk]:
    risks: list[ProjectRisk] = []
    for d in deliverable_rows:
        if d.due_date and d.due_date < on and d.status not in DONE:
            risks.append(
                ProjectRisk(
                    kind="deliverable_overdue", message=f"{d.name} was due on {d.due_date}", ref_id=d.id
                )
            )
    soon = on + timedelta(days=ROLE_SOON_DAYS)
    for r in role_rows:
        if (
            r.status == "open"
            and r.start_date is not None
            and r.start_date <= soon
            and (r.end_date is None or r.end_date >= on)
        ):
            risks.append(
                ProjectRisk(
                    kind="role_unfilled",
                    message=f"{r.title} starts {r.start_date} and is unfilled",
                    ref_id=r.id,
                )
            )
    for pid, name in over_workers:
        risks.append(
            ProjectRisk(
                kind="over_allocation", message=f"{name or 'A Worker'} is allocated above 100%", ref_id=pid
            )
        )
    for b in burn_rows:
        if b.pct is not None and b.pct >= BUDGET_RISK_PCT:
            risks.append(
                ProjectRisk(kind="budget_threshold", message=f"{b.dimension} budget at {b.pct.normalize()}%")
            )
    if (
        sow is not None
        and sow.status == "active"
        and sow.end_date
        and on <= sow.end_date <= on + timedelta(days=SOW_SOON_DAYS)
    ):
        risks.append(
            ProjectRisk(
                kind="sow_ending", message=f"SOW {sow.human_id} ends on {sow.end_date}", ref_id=sow.id
            )
        )
    return risks


async def project_360(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> Project360Out:
    from app.services.commercials import milestones

    project = await get_project(ctx, db, project_id)
    on = today()
    sow = (
        (await db.execute(select(Contract).where(Contract.id == project.contract_id))).scalar_one_or_none()
        if project.contract_id
        else None
    )
    role_rows = list(
        (
            await db.execute(
                select(ProjectRole)
                .where(ProjectRole.project_id == project.id)
                .order_by(ProjectRole.start_date.nulls_last(), ProjectRole.created_at)
            )
        ).scalars()
    )
    deliverable_rows = list(
        (
            await db.execute(
                select(Deliverable)
                .where(Deliverable.project_id == project.id)
                .order_by(Deliverable.due_date.nulls_last(), Deliverable.created_at)
            )
        ).scalars()
    )
    burn_rows = await burn.burn_rows(db, project.id)
    assignment_out = await assignments.list_for_project(ctx, db, project.id)
    names = {a.candidate_id: a.worker_name for a in assignment_out if a.candidate_id and a.worker_name}
    lo, hi = timeline.window(None, None, max(on, project.start_date) if project.start_date else on)
    tl = await timeline.load_timeline(db, project, lo, hi, names)  # type: ignore[arg-type]
    over = [(w.placement_id, w.worker_name) for w in tl.workers if any(s.over for s in w.segments)]
    return Project360Out(
        project=await projects.out(db, project),
        sow=_sow_out(sow),
        roles=await roles.outs(ctx, db, role_rows),
        assignments=assignment_out,
        gap_count=tl.gap_count,
        over_allocation_count=tl.over_allocation_count,
        milestones=await milestones.list_milestones(ctx, db, project_id=project.id)
        if project.contract_id
        else [],
        deliverables=await deliverables.outs(db, deliverable_rows),
        budgets=[budgets.out(b) for b in await budgets.rows_for(db, project.id)],
        burn=[burn._burn_out(b) for b in burn_rows],
        invoices=await invoices_for(db, project),
        risks=_risks(
            on,
            deliverable_rows=deliverable_rows,
            role_rows=role_rows,
            burn_rows=burn_rows,
            sow=sow,
            over_workers=over,
        ),
    )


async def get_timeline(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, start: date | None, end: date | None
) -> timeline.TimelineOut:
    project = await get_project(ctx, db, project_id)
    lo, hi = timeline.window(start, end, project.start_date or today())
    rows = (
        await db.execute(
            select(ProjectAssignment.candidate_id).where(
                ProjectAssignment.project_id == project.id, ProjectAssignment.candidate_id.is_not(None)
            )
        )
    ).scalars()
    cids = set(rows)
    names = (
        dict(
            (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cids)))).all()
        )
        if cids
        else {}
    )
    return await timeline.load_timeline(db, project, lo, hi, names)


async def risk_counts(db: AsyncSession, rows: Sequence[Project]) -> dict[uuid.UUID, int]:
    if not rows:
        return {}
    ids = [r.id for r in rows]
    on = today()
    counts: Counter[uuid.UUID] = Counter()
    for (pid,) in (
        await db.execute(
            select(Deliverable.project_id).where(
                Deliverable.project_id.in_(ids), Deliverable.due_date < on, Deliverable.status.not_in(DONE)
            )
        )
    ).all():
        counts[pid] += 1
    soon = on + timedelta(days=ROLE_SOON_DAYS)
    for (pid,) in (
        await db.execute(
            select(ProjectRole.project_id).where(
                ProjectRole.project_id.in_(ids),
                ProjectRole.status == "open",
                ProjectRole.start_date <= soon,
                or_(ProjectRole.end_date.is_(None), ProjectRole.end_date >= on),
            )
        )
    ).all():
        counts[pid] += 1
    for (pid,) in (
        await db.execute(
            select(ProjectBurn.project_id).where(
                ProjectBurn.project_id.in_(ids), ProjectBurn.pct >= BUDGET_RISK_PCT
            )
        )
    ).all():
        counts[pid] += 1
    contract_ids = {r.contract_id for r in rows if r.contract_id}
    if contract_ids:
        ending = set(
            (
                await db.execute(
                    select(Contract.id).where(
                        Contract.id.in_(contract_ids),
                        Contract.status == "active",
                        Contract.end_date >= on,
                        Contract.end_date <= on + timedelta(days=SOW_SOON_DAYS),
                    )
                )
            ).scalars()
        )
        for r in rows:
            if r.contract_id in ending:
                counts[r.id] += 1
    return dict(counts)
