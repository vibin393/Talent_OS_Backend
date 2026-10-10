"""AD-22: ``enable_period_guard`` -- rows dated in a missing or closed accounting period cannot be written at the
database (Epic 13). Scratch table created by the owner role, exercised as ``talent_app``."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.db_errors import translate
from app.core.fiscal import today_for
from app.core.migration_helpers import enable_period_guard_sql
from app.models.identity import Tenant
from app.services import legal_entities_service
from tests.core.guard_helpers import create_scratch, drop_scratch, sqlstate
from tests.factories import create_tenant_row

DOCS = "t13_period_docs"


@pytest.fixture(scope="module")
async def table(owner_db: AsyncEngine) -> AsyncIterator[None]:
    await create_scratch(
        owner_db,
        DOCS,
        "legal_entity_id uuid NOT NULL, doc_date date, note text",
        enable_period_guard_sql(DOCS, date_column="doc_date", allowed_columns=("note",)),
    )
    yield
    await drop_scratch(owner_db, DOCS)


async def _entity() -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = await create_tenant_row()
    async with tenant_session(tenant_id) as db:
        tenant = await db.get(Tenant, tenant_id)
        entity = await legal_entities_service.provision_default_entity(db, tenant)
    return tenant_id, entity.id


async def _insert(tenant: uuid.UUID, le: uuid.UUID, doc_date: date | None) -> uuid.UUID:
    row = uuid.uuid4()
    async with tenant_session(tenant) as s:
        await s.execute(
            text(f"INSERT INTO {DOCS} (id, tenant_id, legal_entity_id, doc_date) VALUES (:id, :t, :le, :d)"),
            {"id": row, "t": tenant, "le": le, "d": doc_date},
        )
    return row


def _close_ctx(tenant: uuid.UUID) -> tuple[RequestContext, ActorRef]:
    user = uuid.uuid4()
    return RequestContext(tenant_id=tenant), ActorRef("user", user)


async def test_open_month_accepts_missing_month_refuses(table: None) -> None:
    tenant, le = await _entity()
    today = today_for("Asia/Kolkata")
    await _insert(tenant, le, today)
    await _insert(tenant, le, None)  # drafts without a date are not guarded
    with pytest.raises(DBAPIError) as exc:
        await _insert(tenant, le, date(2031, 1, 15))
    assert sqlstate(exc.value) == "TOS01"
    domain = translate(exc.value)
    assert domain is not None and domain.code == "period_closed"
    assert domain.details == {"legal_entity_id": str(le), "period": "2031-01"}


async def test_closed_month_refuses_insert_update_delete(table: None) -> None:
    tenant, le = await _entity()
    today = today_for("Asia/Kolkata")
    row = await _insert(tenant, le, today)
    ctx, actor = _close_ctx(tenant)
    async with tenant_session(tenant) as db:
        closed = await legal_entities_service.close(ctx, db, le, today.year, today.month, actor=actor)
    assert closed.status == "closed"
    with pytest.raises(DBAPIError) as exc:
        await _insert(tenant, le, today)
    assert sqlstate(exc.value) == "TOS01"
    async with tenant_session(tenant) as s:
        # Allow-listed columns may still change.
        await s.execute(text(f"UPDATE {DOCS} SET note = 'paid' WHERE id = :id"), {"id": row})
    for sql in (f"UPDATE {DOCS} SET doc_date = :d WHERE id = :id", f"DELETE FROM {DOCS} WHERE id = :id"):
        params: dict[str, object] = {"id": row, "d": date(2031, 1, 1)}
        with pytest.raises(DBAPIError) as exc2:
            async with tenant_session(tenant) as s:
                await s.execute(text(sql), params)
        assert sqlstate(exc2.value) == "TOS01"
    # Moving a row OUT of a closed month is refused too (the OLD date is checked).
    async with tenant_session(tenant) as db:
        await legal_entities_service.reopen(ctx, db, le, today.year, today.month, actor=actor, reason="fix")
    async with tenant_session(tenant) as s:
        await s.execute(text(f"DELETE FROM {DOCS} WHERE id = :id"), {"id": row})


async def test_guard_waits_for_concurrent_close(table: None) -> None:
    """An insert holding the period FOR SHARE blocks a concurrent close until it commits; afterwards inserts
    into that month are refused."""
    tenant, le = await _entity()
    today = today_for("Asia/Kolkata")
    inserted = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def writer() -> None:
        async with tenant_session(tenant) as s:
            await s.execute(
                text(f"INSERT INTO {DOCS} (tenant_id, legal_entity_id, doc_date) VALUES (:t, :le, :d)"),
                {"t": tenant, "le": le, "d": today},
            )
            inserted.set()
            await release.wait()
            order.append("writer_commit")

    async def closer() -> None:
        await inserted.wait()
        ctx, actor = _close_ctx(tenant)
        async with tenant_session(tenant) as db:
            await legal_entities_service.close(ctx, db, le, today.year, today.month, actor=actor)
        order.append("closed")

    async def releaser() -> None:
        await inserted.wait()
        await asyncio.sleep(0.5)
        order.append("release")
        release.set()

    await asyncio.gather(writer(), closer(), releaser())
    assert order.index("writer_commit") < order.index("closed")
    with pytest.raises(DBAPIError):
        await _insert(tenant, le, today)
