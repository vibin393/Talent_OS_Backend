"""Facade of the Public API adapters (Epic 67, spec 0.6). ``router/public_v1/*`` call these only."""

import uuid
from datetime import date

from app.core.database import tenant_session
from app.services.public_api import (
    applications,
    candidates,
    delivery,
    demands,
    deprecations,
    directory,
    engagement,
    finance,
    imports,
    ingest,
)
from app.services.public_api._core import Order
from app.services.public_api.imports import process_import


async def deprecation_report_all() -> dict[str, int]:
    from app.tasks.runtime import tenant_ids

    out: dict[str, int] = {}
    for tid in await tenant_ids():
        async with tenant_session(tid) as db:
            out[str(tid)] = await deprecations.report_tenant(db, tid)
    return out


async def deprecation_report_tenant(tenant_id: uuid.UUID, today: date | None = None) -> int:
    async with tenant_session(tenant_id) as db:
        return await deprecations.report_tenant(db, tenant_id, today)


__all__ = [
    "Order",
    "applications",
    "candidates",
    "delivery",
    "demands",
    "deprecation_report_all",
    "deprecation_report_tenant",
    "deprecations",
    "directory",
    "engagement",
    "finance",
    "imports",
    "ingest",
    "process_import",
]
