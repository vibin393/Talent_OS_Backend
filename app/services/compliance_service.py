# ruff: file-ignore[line-too-long]
"""Facade of the ``compliance`` domain (Epic 13 contract layer; filled by Epic 21). Other domains import from here.

``worker_compliance(ctx, db, candidate_id, on_date) -> WorkerComplianceStatus`` keeps its frozen signature
(``tests/contracts/signatures.json``).
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.compliance import WorkerComplianceStatus
from app.services.compliance import dashboard


async def worker_compliance(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, on_date: date
) -> WorkerComplianceStatus:
    return await dashboard.worker_compliance(ctx, db, candidate_id, on_date)


__all__ = ["worker_compliance"]
