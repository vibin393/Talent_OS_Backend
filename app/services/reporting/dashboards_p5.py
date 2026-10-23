# ruff: file-ignore[line-too-long]
"""Phase 5 dashboard projections for reporting (Epic 79 story 79.3, FR-407).

Read-only aggregates over the ``p5tables`` projections for the "Ecosystem" dashboard tiles that do not need a Phase 5
facade. Tiles needing platform tables (Job Network) are served by ``ecosystem_governance.dashboards`` (AD-87), not
here; reporting imports **no** Phase 5 module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from app.services.reporting import p5tables

if TYPE_CHECKING:
    import datetime as dt

    from sqlalchemy.ext.asyncio import AsyncSession

U, W, C = p5tables.usage_counters, p5tables.webhook_deliveries, p5tables.connector_sync_state


async def api_requests_by_installation(db: AsyncSession, tenant_id, date_from: dt.date, date_to: dt.date) -> list[dict]:  # ruff: ignore[missing-type-function-argument]
    stmt = (
        sa.select(
            U.c.installation_id,
            sa.func.sum(U.c.count).label("requests"),
            sa.func.sum(U.c.error_count).label("errors"),
        )
        .where(U.c.tenant_id == tenant_id, U.c.unit == "api_request", U.c.day >= date_from, U.c.day <= date_to)
        .group_by(U.c.installation_id)
    )
    return [dict(r) for r in (await db.execute(stmt)).mappings()]


async def connector_health_by_category(db: AsyncSession, tenant_id) -> list[dict]:  # ruff: ignore[missing-type-function-argument]
    stmt = (
        sa.select(C.c.entity_type, sa.func.sum(C.c.consecutive_failures).label("failures"))
        .where(C.c.tenant_id == tenant_id)
        .group_by(C.c.entity_type)
    )
    return [dict(r) for r in (await db.execute(stmt)).mappings()]


async def webhook_success_rate(db: AsyncSession, tenant_id, date_from: dt.date, date_to: dt.date) -> dict:  # ruff: ignore[missing-type-function-argument]
    stmt = sa.select(
        sa.func.count().label("total"),
        sa.func.sum(sa.case((W.c.outcome == "delivered", 1), else_=0)).label("delivered"),
    ).where(W.c.tenant_id == tenant_id, W.c.created_at >= date_from, W.c.created_at <= date_to)
    row = (await db.execute(stmt)).mappings().first() or {"total": 0, "delivered": 0}
    total = int(row["total"] or 0)
    delivered = int(row["delivered"] or 0)
    return {"total": total, "delivered": delivered, "success_rate": (delivered / total) if total else None}


__all__ = ["api_requests_by_installation", "connector_health_by_category", "webhook_success_rate"]
