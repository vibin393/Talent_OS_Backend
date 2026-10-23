# ruff: file-ignore[line-too-long]
"""Outbox sink for index sync (Epic 75, spec A3; AD-87). Registered from ``hooks.py`` with
``public_events_service.register_sink('job_network', on_rows, event_types={posting.published, posting.closed,
posting.updated, demand.status_changed}, include_internal=True)``.

``on_rows`` is idempotent: for each row of a joined, non-sandbox Tenant it enqueues ``job_network.sync_posting`` on the
``ecosystem`` queue (deduplicated per (tenant, demand) for 30 s). It never queries tenant data -- membership is read
from the platform ``network_members`` table only."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.models.job_network import NetworkMember
from app.services.public_events.outbox import OutboxRow

EVENT_TYPES = frozenset({"posting.published", "posting.closed", "posting.updated", "demand.status_changed"})
_DEDUP_SECONDS = 30


async def _is_active_member(tenant_id: uuid.UUID) -> bool:
    async with platform_session() as pdb:
        row = (
            await pdb.execute(
                select(NetworkMember.id).where(
                    NetworkMember.tenant_id == tenant_id,
                    NetworkMember.status == "active",
                    NetworkMember.verification_status == "verified",
                )
            )
        ).first()
    return row is not None


def _dedup(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> bool:
    """Return True when this (tenant, demand) sync should be enqueued now (Redis ``SET NX EX``; True without Redis)."""
    key = f"jn:sync:{tenant_id}:{demand_id}"
    try:
        from app.services.public_events.dispatch import _redis

        return bool(_redis().set(key, "1", nx=True, ex=_DEDUP_SECONDS))
    except Exception:
        return True


async def on_rows(ctx: RequestContext, db: AsyncSession, rows: Sequence[OutboxRow]) -> None:  # ruff: ignore[unused-function-argument] - db is the tenant session; the sink never reads tenant data
    if not rows:
        return
    tenant_id = rows[0].tenant_id
    if not await _is_active_member(tenant_id):
        return  # non-joined / unverified tenant: acknowledged and ignored
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    seen: set[uuid.UUID] = set()
    for row in rows:
        raw = (row.data or {}).get("demand_id")
        if not raw:
            continue
        try:
            demand_id = uuid.UUID(str(raw))
        except (ValueError, TypeError):
            continue
        if demand_id in seen:
            continue
        seen.add(demand_id)
        if _dedup(tenant_id, demand_id):
            enqueue(tasks.sync_posting, str(tenant_id), str(demand_id))


__all__ = ["EVENT_TYPES", "on_rows"]
