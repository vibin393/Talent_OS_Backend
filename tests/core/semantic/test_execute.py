"""Semantic executor: read-only savepoint, timeout, tenant isolation and scope on real rows (36.2)."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.core.permissions import Scope
from app.core.semantic import (
    FieldDef,
    ScopeSpec,
    SemanticTimeout,
    compile,
    count,
    execute,
    ids,
    register_semantic_source,
    validate,
)
from app.core.semantic import executor as semantic_executor
from app.core.semantic.executor import read_only
from app.core.semantic.registry import unregister_semantic_source
from app.models.candidates import Candidate, Tag
from tests.core.semantic.conftest import World, sctx

SRC = "candidates.candidate"
SLOW = "testsem.slow"


@pytest.fixture
def slow_source() -> Iterator[str]:
    register_semantic_source(
        SLOW,
        Candidate,
        {"slow": FieldDef("bool", "Slow", "none", lambda c: func.pg_sleep(2).is_(None), frozenset({"eq"}))},
        {},
        ScopeSpec(permission="candidate:read", owner_cols=(lambda c: c.owner_id,)),
        label="Slow",
        subject=lambda c: c.id,
    )
    yield SLOW
    unregister_semantic_source(SLOW)


async def test_ids_count_and_rows(world: World) -> None:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        defn = validate({"source": SRC, "filters": {"field": "city", "op": "eq", "value": "Chennai"}})
        found = await ids(defn, ctx)
        assert set(found) == {world.c["c1"], world.c["c3"]}
        assert found == sorted(found)
        assert await count(defn, ctx) == 2
        assert await ids(defn, ctx, limit=1) == found[:1]
        cq = await compile(validate({**defn.model_dump(by_alias=True), "fields": ["full_name", "city"]}), ctx)
        result = await execute(cq, ctx, limit=1)
        assert result.total == 2 and result.truncated and len(result.rows) == 1
        assert result.rows[0]["city"] == "Chennai"
        assert set(result.rows[0]) == {"subject_id", "full_name", "city"}


async def test_cross_tenant_returns_nothing_from_other_tenant(world: World) -> None:
    defn = validate({"source": SRC, "filters": {"field": "full_name", "op": "eq", "value": "Asha Rao"}})
    async with tenant_session(world.tenant_b) as db:
        # Tenant A's principal / context run against tenant B's session: RLS + tenant filter return nothing.
        assert await ids(defn, sctx(db, world.principal(), world.tenant_a)) == []
    async with tenant_session(world.tenant_a) as db:
        assert await ids(defn, sctx(db, world.principal(), world.tenant_a)) == [world.c["c1"]]


@pytest.mark.parametrize(
    ("scope", "visible"),
    [
        (Scope.OWN, {"c1"}),
        (Scope.TEAM, {"c1", "c2"}),
        (Scope.BU, {"c1", "c2", "c3"}),
        (Scope.TENANT, {"c1", "c2", "c3", "c4"}),
    ],
)
async def test_scope_on_rows(world: World, scope: Scope, visible: set[str]) -> None:
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal("u1", candidate__read=scope), world.tenant_a)
        found = set(await ids(validate({"source": SRC}), ctx))
    assert found == {world.c[k] for k in visible}


async def test_system_context_sees_all(world: World) -> None:
    from app.core.semantic import SemanticContext

    async with tenant_session(world.tenant_a) as db:
        ctx = SemanticContext.for_system(world.tenant_a, db, "refresh")
        found = set(await ids(validate({"source": SRC}), ctx))
    assert found == {world.c[k] for k in ("c1", "c2", "c3", "c4")}


async def test_read_only_savepoint_and_outer_transaction_writable(world: World) -> None:
    async with tenant_session(world.tenant_a) as db:
        with pytest.raises(DBAPIError) as info:
            async with read_only(db):
                await db.execute(
                    text("INSERT INTO tags (id, tenant_id, name) VALUES (gen_random_uuid(), :t, 'ro-test')"),
                    {"t": world.tenant_a},
                )
        assert "read-only" in str(info.value)
        assert (await db.execute(text("SHOW transaction_read_only"))).scalar_one() == "off"
        assert (await db.execute(text("SHOW statement_timeout"))).scalar_one() != "30s"
        name = f"rw-{uuid.uuid4().hex[:6]}"
        db.add(Tag(tenant_id=world.tenant_a, name=name))
        await db.flush()
    async with tenant_session(world.tenant_a) as db:
        assert (
            await db.execute(select(func.count()).select_from(Tag).where(Tag.name == name))
        ).scalar_one() == 1


async def test_timeout_raises_query_timeout(
    world: World, slow_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_executor, "STATEMENT_TIMEOUT", "200ms")
    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        defn = validate({"source": slow_source, "filters": {"field": "slow", "op": "eq", "value": True}})
        with pytest.raises(SemanticTimeout) as info:
            await count(defn, ctx)
        assert info.value.status_code == 503 and info.value.code == "query_timeout"
        # The outer transaction is still usable.
        assert (await db.execute(text("SELECT 1"))).scalar_one() == 1


async def test_limits(world: World) -> None:
    from app.core.semantic import SemanticError

    async with tenant_session(world.tenant_a) as db:
        ctx = sctx(db, world.principal(), world.tenant_a)
        cq = await compile(validate({"source": SRC}), ctx)
        with pytest.raises(SemanticError):
            await execute(cq, ctx, limit=100_001)
        with pytest.raises(SemanticError):
            await execute(cq, ctx, limit=0)
