"""AD-21 + AD-23: ``enable_source_lock`` -- a source row with a non-released posting cannot change (Epic 13)."""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.migration_helpers import enable_source_lock_sql
from app.services import postings
from tests.core.guard_helpers import create_scratch, drop_scratch, sqlstate
from tests.factories import create_tenant_row

ENTRIES = "t13_lock_entries"
KIND = "t13_lock_entry"


@pytest.fixture(scope="module")
async def table(owner_db: AsyncEngine) -> AsyncIterator[None]:
    await create_scratch(
        owner_db,
        ENTRIES,
        "hours numeric NOT NULL, note text",
        enable_source_lock_sql(ENTRIES, source_kind=KIND, allowed_columns=("note",)),
    )
    if KIND not in postings.posting_sources():
        postings.register_posting_source(KIND, None, ledgers=frozenset({"billing", "payable"}))
    yield
    await drop_scratch(owner_db, ENTRIES)


def _line(source_id: uuid.UUID, ledger: str) -> postings.PostingLine:
    return postings.PostingLine(
        source_kind=KIND,
        source_id=source_id,
        target_kind="invoice_line" if ledger == "billing" else "payroll_row",
        target_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        quantity=Decimal("8"),
        amount=Decimal("800.00"),
        currency="INR",
        payee_kind=None if ledger == "billing" else "worker",
    )


async def _update(tenant: uuid.UUID, sql: str, entry: uuid.UUID, *, org: OrgScope | None = None) -> None:
    async with tenant_session(tenant, org=org) as s:
        await s.execute(text(sql), {"id": entry})


@pytest.mark.parametrize("ledger", ["billing", "payable"])
async def test_posted_source_rows_are_locked(table: None, ledger: str) -> None:
    tenant = await create_tenant_row()
    entry = uuid.uuid4()
    async with tenant_session(tenant) as s:
        await s.execute(text(f"INSERT INTO {ENTRIES} (id, tenant_id, hours) VALUES (:id, :t, 8)"), {"id": entry, "t": tenant})
    # Unposted: editable.
    await _update(tenant, f"UPDATE {ENTRIES} SET hours = 9 WHERE id = :id", entry)
    ctx = RequestContext(tenant_id=tenant)
    async with tenant_session(tenant) as db:
        ids = await postings.reserve(ctx, db, ledger, [_line(entry, ledger)])  # type: ignore[arg-type]
    for sql in (f"UPDATE {ENTRIES} SET hours = 10 WHERE id = :id", f"DELETE FROM {ENTRIES} WHERE id = :id"):
        with pytest.raises(DBAPIError) as exc:
            await _update(tenant, sql, entry)
        assert sqlstate(exc.value) == "TOS02"
    # An external (worker) session cannot bypass the lock because it cannot see the ledger.
    with pytest.raises(DBAPIError):
        await _update(tenant, f"UPDATE {ENTRIES} SET hours = 11 WHERE id = :id", entry, org=OrgScope.of("worker", uuid.uuid4()))
    # Allow-listed columns stay editable.
    await _update(tenant, f"UPDATE {ENTRIES} SET note = 'checked' WHERE id = :id", entry)
    # Released: editable again.
    async with tenant_session(tenant) as db:
        assert await postings.release(ctx, db, ledger, reason="discarded", posting_ids=ids) == 1  # type: ignore[arg-type]
    await _update(tenant, f"UPDATE {ENTRIES} SET hours = 12 WHERE id = :id", entry)
