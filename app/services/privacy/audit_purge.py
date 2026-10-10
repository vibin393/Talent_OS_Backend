"""Story 22.9 (A10, FR-180): audit-row retention purge (owner role, CLI only).

``python -m app.cli purge-audit --tenant <uuid> [--dry-run]`` deletes the tenant's ``audit_logs`` rows older
than ``retention_policies.audit_retention_years`` (default 7). ``audit_logs`` is append-only for the app role
(UPDATE / DELETE revoked), so the purge runs on the owner engine and is never reachable from a request.

Rows about an entity whose table is bound to a retention class (``retention_bindings()`` -- e.g. BGV
evidence, signed documents, placement / financial records) are refused: their audit trail is part of what that
class retains, so they stay until the class itself expires the record.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import entity_type_of
from app.core.models import Base

DEFAULT_YEARS = 7


def protected_entity_types() -> set[str]:
    """Audit ``entity_type`` values of every table bound to a retention class (plus the bare table names)."""
    from app.core.hooks import autodiscover
    from app.services.privacy.retention_classes import retention_bindings

    autodiscover("app.services")
    tables = set(retention_bindings())
    types = set(tables)
    for mapper in Base.registry.mappers:
        if mapper.local_table is not None and getattr(mapper.local_table, "name", None) in tables:
            types.add(entity_type_of(mapper.class_))
    return types


def _cutoff(now: datetime, years: int) -> datetime:
    try:
        return now.replace(year=now.year - years)
    except ValueError:  # 29 Feb
        return now.replace(year=now.year - years, day=28)


async def purge_audit(
    engine: AsyncEngine, tenant_id: uuid.UUID, *, now: datetime | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Delete (or count, with ``dry_run``) the tenant's audit rows past the retention period.

    Returns ``{years, cutoff, deleted, protected}``: ``protected`` counts old rows refused (entity
    bound to a retention class)."""
    now = now or datetime.now(UTC)
    protected = sorted(protected_entity_types())
    async with engine.begin() as conn:
        years = (
            await conn.execute(
                text("SELECT audit_retention_years FROM retention_policies WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one_or_none() or DEFAULT_YEARS
        cutoff = _cutoff(now, int(years))
        params = {"t": tenant_id, "cutoff": cutoff, "protected": protected}
        old = "tenant_id = :t AND occurred_at < :cutoff"
        guarded = f"{old} AND entity_type = ANY(:protected)"
        eligible = f"{old} AND (entity_type IS NULL OR NOT (entity_type = ANY(:protected)))"
        refused = (
            await conn.execute(text(f"SELECT count(*) FROM audit_logs WHERE {guarded}"), params)
        ).scalar_one()
        if dry_run:
            deleted = (
                await conn.execute(text(f"SELECT count(*) FROM audit_logs WHERE {eligible}"), params)
            ).scalar_one()
        else:
            deleted = (await conn.execute(text(f"DELETE FROM audit_logs WHERE {eligible}"), params)).rowcount
    return {"years": int(years), "cutoff": cutoff, "deleted": int(deleted), "protected": int(refused)}
