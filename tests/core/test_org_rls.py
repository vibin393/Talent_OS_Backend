"""AD-26: organization isolation is fail-closed (Epic 13, story 13.4).

Scratch tables guarded by ``enable_org_rls`` (client / vendor / worker columns, explicit grants, inherited
columns) are read and written as ``talent_app`` with different org scopes; the foundation's real hub table
``placements`` is checked with an unset GUC."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core import org_grants
from app.core.actors import ActorRef
from app.core.database import OrgScope, app_engine, platform_session, tenant_session
from app.core.migration_helpers import GrantsRef, InheritRef, enable_org_rls_sql
from tests.core.guard_helpers import create_scratch, drop_scratch, sqlstate
from tests.factories import create_tenant_row

ROWS, GRANTED, CHILD = "t13_org_rows", "t13_org_granted", "t13_org_child"


@pytest.fixture(scope="module")
async def tables(owner_db: AsyncEngine) -> AsyncIterator[None]:
    await create_scratch(
        owner_db,
        ROWS,
        "client_id uuid, vendor_id uuid, candidate_id uuid, name text",
        enable_org_rls_sql(ROWS, client_col="client_id", vendor_col="vendor_id", worker_col="candidate_id"),
    )
    await create_scratch(owner_db, GRANTED, "name text", enable_org_rls_sql(GRANTED, grants=GrantsRef("t13_granted")))
    await create_scratch(
        owner_db,
        CHILD,
        f"parent_id uuid NOT NULL REFERENCES {ROWS}(id), client_id uuid, vendor_id uuid, candidate_id uuid",
        enable_org_rls_sql(
            CHILD,
            client_col="client_id",
            vendor_col="vendor_id",
            worker_col="candidate_id",
            inherit_from=InheritRef(ROWS, "parent_id", ("client_id", "vendor_id", "candidate_id")),
        ),
    )
    yield
    await drop_scratch(owner_db, CHILD, ROWS, GRANTED)


async def _names(tenant: uuid.UUID, org: OrgScope | None, table: str = ROWS) -> set[str]:
    async with tenant_session(tenant, org=org) as s:
        return set((await s.execute(text(f"SELECT name FROM {table}"))).scalars())


async def _seed(tenant: uuid.UUID) -> dict[str, uuid.UUID]:
    ids = {k: uuid.uuid4() for k in ("client_a", "client_b", "vendor_v", "worker_w")}
    rows = [
        ("a1", ids["client_a"], None, None),
        ("a2", ids["client_a"], ids["vendor_v"], ids["worker_w"]),
        ("b1", ids["client_b"], None, None),
        ("internal", None, None, None),
    ]
    async with tenant_session(tenant) as s:
        for name, client, vendor, worker in rows:
            await s.execute(
                text(f"INSERT INTO {ROWS} (tenant_id, client_id, vendor_id, candidate_id, name) VALUES (:t, :c, :v, :w, :n)"),
                {"t": tenant, "c": client, "v": vendor, "w": worker, "n": name},
            )
    return ids


async def test_unset_guc_sees_nothing(tables: None) -> None:
    tenant = uuid.uuid4()
    await _seed(tenant)
    async with app_engine().connect() as conn:  # tenant set, org GUC unset
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        assert (await conn.execute(text(f"SELECT count(*) FROM {ROWS}"))).scalar_one() == 0
        await conn.rollback()
    async with platform_session() as s:
        assert (await s.execute(text(f"SELECT count(*) FROM {ROWS}"))).scalar_one() == 0


async def test_internal_sees_all(tables: None) -> None:
    tenant = uuid.uuid4()
    await _seed(tenant)
    assert await _names(tenant, None) == {"a1", "a2", "b1", "internal"}
    assert await _names(tenant, OrgScope.internal()) == {"a1", "a2", "b1", "internal"}


async def test_client_vendor_worker_see_only_their_rows(tables: None) -> None:
    tenant = uuid.uuid4()
    ids = await _seed(tenant)
    assert await _names(tenant, OrgScope.of("client", ids["client_a"])) == {"a1", "a2"}
    assert await _names(tenant, OrgScope.of("client", ids["client_b"])) == {"b1"}
    assert await _names(tenant, OrgScope.of("vendor", ids["vendor_v"])) == {"a2"}
    assert await _names(tenant, OrgScope.of("worker", ids["worker_w"])) == {"a2"}
    assert await _names(tenant, OrgScope.of("client", uuid.uuid4())) == set()


async def test_external_writes_limited_to_own_org(tables: None) -> None:
    tenant = uuid.uuid4()
    ids = await _seed(tenant)
    client_a = OrgScope.of("client", ids["client_a"])
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(tenant, org=client_a) as s:
            await s.execute(
                text(f"INSERT INTO {ROWS} (tenant_id, client_id, name) VALUES (:t, :c, 'smuggled')"),
                {"t": tenant, "c": ids["client_b"]},
            )
    async with tenant_session(tenant, org=client_a) as s:
        updated = await s.execute(text(f"UPDATE {ROWS} SET name = 'hacked' WHERE name = 'b1'"))
        assert updated.rowcount == 0  # type: ignore[attr-defined]


async def test_grants_open_single_rows(tables: None) -> None:
    tenant = await create_tenant_row()
    client = uuid.uuid4()
    shown, hidden = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(tenant) as s:
        for rid, name in ((shown, "shown"), (hidden, "hidden")):
            await s.execute(text(f"INSERT INTO {GRANTED} (id, tenant_id, name) VALUES (:id, :t, :n)"), {"id": rid, "t": tenant, "n": name})
        await org_grants.grant(s, "client", client, "t13_granted", shown, ActorRef("system", None))
        await org_grants.grant(s, "client", client, "t13_granted", shown, ActorRef("system", None))  # idempotent
        assert await org_grants.has_grant(s, "client", client, "t13_granted", shown)
    assert await _names(tenant, OrgScope.of("client", client), GRANTED) == {"shown"}
    assert await _names(tenant, OrgScope.of("client", uuid.uuid4()), GRANTED) == set()
    assert await _names(tenant, OrgScope.of("vendor", client), GRANTED) == set()
    async with tenant_session(tenant) as s:
        await org_grants.revoke(s, "client", client, "t13_granted", shown)
    assert await _names(tenant, OrgScope.of("client", client), GRANTED) == set()


async def test_inherit_trigger_copies_org_columns(tables: None) -> None:
    tenant = uuid.uuid4()
    ids = await _seed(tenant)
    async with tenant_session(tenant) as s:
        parent = (await s.execute(text(f"SELECT id FROM {ROWS} WHERE name = 'a2'"))).scalar_one()
        await s.execute(text(f"INSERT INTO {CHILD} (tenant_id, parent_id) VALUES (:t, :p)"), {"t": tenant, "p": parent})
        row = (await s.execute(text(f"SELECT client_id, vendor_id, candidate_id FROM {CHILD} WHERE parent_id = :p"), {"p": parent})).one()
    assert tuple(row) == (ids["client_a"], ids["vendor_v"], ids["worker_w"])
    async with tenant_session(tenant, org=OrgScope.of("client", ids["client_b"])) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM {CHILD}"))).scalar_one() == 0
    # A parent the caller cannot see counts as missing (fail-closed).
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant, org=OrgScope.of("client", ids["client_b"])) as s:
            await s.execute(text(f"INSERT INTO {CHILD} (tenant_id, parent_id) VALUES (:t, :p)"), {"t": tenant, "p": parent})
    assert sqlstate(exc.value) == "TOS03"


async def test_real_hub_tables_fail_closed() -> None:
    tenant = await create_tenant_row()
    async with app_engine().connect() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        for table in ("placements", "invoices", "contracts", "approval_requests", "notifications", "billing_postings"):
            assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
        await conn.rollback()


def test_org_scope_validation() -> None:
    with pytest.raises(ValueError):
        OrgScope("client")
    with pytest.raises(ValueError):
        OrgScope.of("internal", uuid.uuid4())
    with pytest.raises(ValueError):
        OrgScope("partner", uuid.uuid4())
