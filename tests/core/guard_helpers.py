"""Shared helpers for the Epic 13 database-guard tests: scratch tables created by the owner role exactly like a
migration would (``enable_rls`` + the guard helpers), then exercised as ``talent_app``."""

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.migration_helpers import enable_rls_sql


async def create_scratch(owner_db: AsyncEngine, name: str, columns: str, guards: Iterable[str] = ()) -> None:
    async with owner_db.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        await conn.execute(
            text(
                f"CREATE TABLE {name} (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, "
                f"{columns}, updated_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        for stmt in enable_rls_sql(name):
            await conn.execute(text(stmt))
        for stmt in guards:
            await conn.execute(text(stmt))


async def drop_scratch(owner_db: AsyncEngine, *names: str) -> None:
    async with owner_db.begin() as conn:
        for name in names:
            await conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))


def sqlstate(exc: BaseException) -> str | None:
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 10:
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if code:
            return str(code)
        cur = getattr(cur, "orig", None) or cur.__cause__
        seen += 1
    return None
