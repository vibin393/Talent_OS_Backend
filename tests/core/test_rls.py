"""AD-2: tenant isolation holds without any WHERE clause, for the non-superuser runtime role."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.context import update_context
from app.core.database import DBSessionDep, app_engine, platform_session, tenant_session
from app.core.tenancy import PLATFORM_TABLES, requires_rls
from app.main import create_app


async def _insert(table: str, tenant_id: uuid.UUID, name: str, *, as_tenant: uuid.UUID | None = None) -> None:
    async with tenant_session(as_tenant or tenant_id) as s:
        await s.execute(
            text(f"INSERT INTO {table} (tenant_id, name) VALUES (:t, :n)"), {"t": tenant_id, "n": name}
        )


async def test_runtime_role_is_not_privileged() -> None:
    async with platform_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT current_user, r.rolsuper, r.rolbypassrls FROM pg_roles r "
                    "WHERE r.rolname = current_user"
                )
            )
        ).one()
    assert tuple(row) == ("talent_app", False, False)


async def test_rls_hides_other_tenants_rows(rls_table: str) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _insert(rls_table, tenant_a, "a-row")

    async with tenant_session(tenant_b) as s:
        rows_b = (await s.execute(text(f"SELECT * FROM {rls_table}"))).all()
    async with tenant_session(tenant_a) as s:
        rows_a = (await s.execute(text(f"SELECT * FROM {rls_table}"))).all()

    assert rows_b == []
    assert [r.name for r in rows_a] == ["a-row"]


async def test_rls_blocks_cross_tenant_write(rls_table: str) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(DBAPIError, match="row-level security"):
        await _insert(rls_table, tenant_a, "smuggled", as_tenant=tenant_b)
    async with tenant_session(tenant_a) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM {rls_table}"))).scalar_one() == 0


async def test_rls_blocks_cross_tenant_update_and_delete(rls_table: str) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _insert(rls_table, tenant_a, "keep-me")
    async with tenant_session(tenant_b) as s:
        updated = await s.execute(text(f"UPDATE {rls_table} SET name = 'hacked'"))
        deleted = await s.execute(text(f"DELETE FROM {rls_table}"))
    assert updated.rowcount == 0  # type: ignore[attr-defined]
    assert deleted.rowcount == 0  # type: ignore[attr-defined]
    async with tenant_session(tenant_a) as s:
        assert (await s.execute(text(f"SELECT name FROM {rls_table}"))).scalars().all() == ["keep-me"]


async def test_no_tenant_context_returns_zero_rows(rls_table: str) -> None:
    await _insert(rls_table, uuid.uuid4(), "someone")
    async with platform_session() as s:
        assert (await s.execute(text(f"SELECT * FROM {rls_table}"))).all() == []


async def test_no_tenant_context_after_set_local_on_same_connection(rls_table: str) -> None:
    """A pooled connection that previously ran SET LOCAL reports '' -- must still be 0 rows, not an error."""
    tenant = uuid.uuid4()
    await _insert(rls_table, tenant, "pooled")
    async with app_engine().connect() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        assert (await conn.execute(text(f"SELECT count(*) FROM {rls_table}"))).scalar_one() == 1
        await conn.commit()
        assert (await conn.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one() == ""
        assert (await conn.execute(text(f"SELECT count(*) FROM {rls_table}"))).scalar_one() == 0
        await conn.rollback()


async def test_tenant_context_survives_multiple_transactions_in_one_session(rls_table: str) -> None:
    tenant = uuid.uuid4()
    await _insert(rls_table, tenant, "t1")
    async with tenant_session(tenant) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM {rls_table}"))).scalar_one() == 1
        await s.commit()
        assert (await s.execute(text(f"SELECT count(*) FROM {rls_table}"))).scalar_one() == 1


async def test_every_non_platform_table_has_forced_rls(rls_table: str) -> None:
    """Guard for future modules: every public table not listed in PLATFORM_TABLES / INFRA_TABLES must be
    tenant-owned (``tenant_id NOT NULL``) with ENABLE + FORCE row level security."""
    async with platform_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "  EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
                    "          AND a.attname = 'tenant_id' AND a.attnotnull AND NOT a.attisdropped) "
                    "    AS has_tenant_id "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
                )
            )
        ).all()
    tables = {r.relname: r for r in rows}
    assert tables.keys() >= PLATFORM_TABLES, "platform tables must exist once migrated"
    tenant_owned = {name for name in tables if requires_rls(name)}
    assert tenant_owned >= {"tenant_sequences", "invitations"}
    missing = sorted(
        n
        for n in tenant_owned
        if not (tables[n].relrowsecurity and tables[n].relforcerowsecurity and tables[n].has_tenant_id)
    )
    assert missing == [], f"tables without forced RLS / tenant_id (or add them to PLATFORM_TABLES): {missing}"


async def test_every_tenant_owned_table_references_tenants_with_restrict(rls_table: str) -> None:
    """Every requires_rls() table (except the test-only RLS table) has FK tenant_id -> tenants(id)
    ON DELETE RESTRICT (confdeltype 'r')."""
    async with platform_session() as s:
        tables = (
            (
                await s.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
                    )
                )
            )
            .scalars()
            .all()
        )
        fk_tables = set(
            (
                await s.execute(
                    text(
                        "SELECT c.relname FROM pg_constraint k "
                        "JOIN pg_class c ON c.oid = k.conrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1] "
                        "WHERE n.nspname = 'public' AND k.contype = 'f' "
                        "  AND array_length(k.conkey, 1) = 1 AND a.attname = 'tenant_id' "
                        "  AND k.confrelid = 'public.tenants'::regclass AND k.confdeltype = 'r'"
                    )
                )
            ).scalars()
        )
    tenant_owned = {t for t in tables if requires_rls(t) and t != rls_table}
    assert tenant_owned >= {"tenant_sequences", "invitations"}
    assert sorted(tenant_owned - fk_tables) == []


def test_every_mapped_non_platform_table_uses_tenant_id() -> None:
    from app.core.models import Base

    offenders = sorted(
        t.name
        for t in Base.metadata.tables.values()
        if requires_rls(t.name) and ("tenant_id" not in t.c or t.c.tenant_id.nullable)
    )
    assert offenders == []


async def _tenant_from_header(request: Request) -> None:
    """Stand in for the story 1.3 auth dependency: put the tenant on the request context."""
    if tenant := request.headers.get("x-test-tenant"):
        update_context(tenant_id=uuid.UUID(tenant))


@pytest.fixture
async def dep_client(rls_table: str) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app()

    @app.get("/api/v1/_test/rows", dependencies=[Depends(_tenant_from_header)])
    async def rows(db: DBSessionDep) -> list[str]:
        return list((await db.execute(text(f"SELECT name FROM {rls_table} ORDER BY name"))).scalars())

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_get_db_scopes_request_session_to_context_tenant(
    rls_table: str, dep_client: httpx.AsyncClient
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _insert(rls_table, tenant_a, "dep-a")
    await _insert(rls_table, tenant_b, "dep-b")

    as_a = await dep_client.get("/api/v1/_test/rows", headers={"x-test-tenant": str(tenant_a)})
    assert as_a.status_code == 200
    assert as_a.json() == ["dep-a"]

    no_tenant = await dep_client.get("/api/v1/_test/rows")
    assert no_tenant.status_code == 200
    assert no_tenant.json() == []
