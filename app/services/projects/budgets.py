"""Project budgets (Epic 45, FR-230): hours / cost / revenue per project (internal only).

Changing an amount bumps ``version`` and clears the 80 / 100 % alert stamps (each threshold alerts once per
budget version). Money budgets carry the project currency (INR when the project has no SOW yet).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.money import quantize_amount, quantize_hours
from app.models.projects import ProjectBudget
from app.schemas.projects import BudgetOut, BudgetsIn
from app.services.projects.common import WRITE, get_project, tenant_of, timeline

DEFAULT_CURRENCY = "INR"


def out(row: ProjectBudget) -> BudgetOut:
    return BudgetOut(
        dimension=row.dimension,
        amount=row.amount,
        currency=row.currency,
        version=row.version,
        alert_80_at=row.alert_80_at,
        alert_100_at=row.alert_100_at,
    )


async def rows_for(db: AsyncSession, project_id: uuid.UUID) -> list[ProjectBudget]:
    return list(
        (
            await db.execute(
                select(ProjectBudget)
                .where(ProjectBudget.project_id == project_id)
                .order_by(ProjectBudget.dimension)
            )
        ).scalars()
    )


async def list_budgets(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> list[BudgetOut]:
    await get_project(ctx, db, project_id)
    return [out(r) for r in await rows_for(db, project_id)]


async def set_budgets(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, data: BudgetsIn
) -> list[BudgetOut]:
    project = await get_project(ctx, db, project_id, permission=WRITE, lock=True)
    currency = project.currency or DEFAULT_CURRENCY
    existing = {r.dimension: r for r in await rows_for(db, project.id)}
    changed: list[str] = []
    for item in data.budgets:
        money = item.dimension != "hours"
        amount = quantize_amount(item.amount, currency) if money else quantize_hours(item.amount)
        row = existing.get(item.dimension)
        if row is None:
            row = ProjectBudget(
                id=uuid.uuid4(),
                tenant_id=tenant_of(db),
                project_id=project.id,
                dimension=item.dimension,
                amount=amount,
                currency=currency if money else None,
                version=1,
            )
            db.add(row)
            existing[item.dimension] = row
        elif row.amount != amount:
            row.amount = amount
            row.version += 1
            row.alert_80_at = None
            row.alert_100_at = None
        else:
            continue
        changed.append(item.dimension)
        audit.record(
            "project_budget.changed",
            "project_budget",
            row.id,
            session=db,
            project_id=str(project.id),
            dimension=item.dimension,
            amount=str(amount),
            version=row.version,
        )
    await db.flush()
    if changed:
        await timeline(ctx, db, project.id, f"{project.human_id}: budget updated", {"dimensions": changed})
    return [out(r) for r in await rows_for(db, project.id)]
