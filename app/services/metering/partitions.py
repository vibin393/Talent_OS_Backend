# ruff: noqa: E501
"""Monthly RANGE partitions (Epic 66, NFR-74): a small registry of partitioned tenant tables (``api_request_log``
here; Epic 68 registers ``webhook_deliveries`` from its ``hooks.py``), ``ensure_monthly`` (``partitions.ensure_monthly``,
daily) and the retention purge (``api_request_log.purge``, daily).

DDL runs through the owner role (``core.database.owner_engine``) -- the runtime role cannot create tables -- using
``core.migration_helpers.monthly_partition_sql`` so every partition carries the parent's tenant (and org) RLS policies.
Row deletes run in tenant sessions (RLS is forced on the table owner too).
"""

import dataclasses
import datetime as dt
import re
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.core.database import owner_engine, platform_session, tenant_session
from app.core.migration_helpers import monthly_partition_name, monthly_partition_sql, months_from
from app.models.identity import Tenant

if TYPE_CHECKING:
    import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class PartitionedTable:
    name: str
    org_rls: bool = True
    retention_days: int | None = None
    time_column: str = "at"


_REGISTRY: dict[str, PartitionedTable] = {}
_PARTITION_RE = re.compile(r"_y(\d{4})m(\d{2})$")


def register_partitioned_table(
    name: str, *, org_rls: bool = True, retention_days: int | None = None, time_column: str = "at"
) -> None:
    """Keep ``name`` (RANGE-partitioned by month on ``time_column``) supplied with partitions; with
    ``retention_days`` the purge drops whole months past the window and deletes older rows. Idempotent."""
    _REGISTRY[name] = PartitionedTable(name, org_rls, retention_days, time_column)


def registered_tables() -> tuple[PartitionedTable, ...]:
    return tuple(_REGISTRY.values())


register_partitioned_table("api_request_log", retention_days=30)

_ENGINE_OVERRIDE: dict[str, AsyncEngine | None] = {"engine": None}


def _engine() -> tuple[AsyncEngine, bool]:
    override = _ENGINE_OVERRIDE["engine"]
    if override is not None:
        return override, False
    return owner_engine(poolclass=NullPool), True


async def existing_partitions(table: str) -> list[str]:
    engine, owned = _engine()
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = :t ORDER BY c.relname"
                ),
                {"t": table},
            )
            return [r[0] for r in rows]
    finally:
        if owned:
            await engine.dispose()


async def ensure_monthly(months_ahead: int = 3, *, today: dt.date | None = None) -> dict[str, list[str]]:
    """Create the current month and ``months_ahead`` further monthly partitions of every registered table.
    Returns the partitions created per table (empty when everything already existed)."""
    today = today or dt.datetime.now(dt.UTC).date()
    engine, owned = _engine()
    created: dict[str, list[str]] = {}
    try:
        for spec in registered_tables():
            async with engine.begin() as conn:
                have = {
                    r[0]
                    for r in await conn.execute(
                        text(
                            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = :t"
                        ),
                        {"t": spec.name},
                    )
                }
                made: list[str] = []
                for year, month in months_from(today.year, today.month, months_ahead + 1):
                    name = monthly_partition_name(spec.name, year, month)
                    if name in have:
                        continue
                    for stmt in monthly_partition_sql(spec.name, year, month, org_rls=spec.org_rls):
                        await conn.execute(text(stmt))
                    made.append(name)
            created[spec.name] = made
            if made:
                logger.info("Created partitions {}", ", ".join(made))
    finally:
        if owned:
            await engine.dispose()
    return created


def _partition_end(name: str) -> dt.date | None:
    m = _PARTITION_RE.search(name)
    if m is None:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return dt.date(ny, nm, 1)


async def purge(*, now: dt.datetime | None = None) -> dict[str, dict[str, int]]:
    """Retention: drop partitions that end on/before the cutoff (owner role), then delete older rows per tenant."""
    now = now or dt.datetime.now(dt.UTC)
    out: dict[str, dict[str, int]] = {}
    for spec in registered_tables():
        if not spec.retention_days:
            continue
        cutoff = now - dt.timedelta(days=spec.retention_days)
        dropped = 0
        for name in await existing_partitions(spec.name):
            end = _partition_end(name)
            if end is not None and end <= cutoff.date():
                engine, owned = _engine()
                try:
                    async with engine.begin() as conn:
                        await conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
                finally:
                    if owned:
                        await engine.dispose()
                dropped += 1
                logger.info("Dropped expired partition {}", name)
        deleted = await _delete_rows(spec, cutoff)
        out[spec.name] = {"partitions_dropped": dropped, "rows_deleted": deleted}
    return out


async def _delete_rows(spec: PartitionedTable, cutoff: dt.datetime) -> int:
    from sqlalchemy import column, table

    t = table(spec.name, column(spec.time_column))
    async with platform_session() as pdb:
        tenant_ids: list[uuid.UUID] = list((await pdb.execute(select(Tenant.id))).scalars())
    total = 0
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as db:
                result = await db.execute(delete(t).where(t.c[spec.time_column] < cutoff))
                total += int(result.rowcount or 0)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Retention purge of {} failed for tenant {}", spec.name, tid)
    return total


__all__ = [
    "PartitionedTable",
    "ensure_monthly",
    "existing_partitions",
    "purge",
    "register_partitioned_table",
    "registered_tables",
]
