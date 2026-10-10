"""AD-21: ``enable_immutability`` -- posted rows cannot be edited or deleted at the database (Epic 13).

Scratch tables are created by the owner role and exercised as ``talent_app`` (raw SQL, no service code)."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import OrgScope, tenant_session
from app.core.db_errors import translate
from app.core.migration_helpers import ParentRef, disable_immutability_sql, enable_immutability_sql
from tests.core.guard_helpers import create_scratch, drop_scratch, sqlstate

DOCS, LINES, LEDGER = "t13_imm_docs", "t13_imm_lines", "t13_imm_ledger"


@pytest.fixture(scope="module")
async def tables(owner_db: AsyncEngine) -> AsyncIterator[None]:
    await create_scratch(
        owner_db,
        DOCS,
        "status text NOT NULL, title text, note text, cancel_basis text",
        enable_immutability_sql(
            DOCS,
            status_column="status",
            mutable_states=("draft", "pending_approval"),
            allowed_columns=("note",),
            allowed_transitions=(("issued", "cancelled"),),
            transition_columns=("cancel_basis",),
        ),
    )
    await create_scratch(
        owner_db,
        LINES,
        f"doc_id uuid NOT NULL REFERENCES {DOCS}(id) ON DELETE CASCADE, amount numeric, note text",
        enable_immutability_sql(
            LINES,
            status_column=None,
            allowed_columns=("note",),
            parent=ParentRef(DOCS, "doc_id", "status", ("draft",)),
        ),
    )
    await create_scratch(
        owner_db,
        LEDGER,
        "state text NOT NULL, amount numeric NOT NULL",
        enable_immutability_sql(LEDGER, status_column=None, allowed_columns=("state",)),
    )
    yield
    await drop_scratch(owner_db, LINES, DOCS, LEDGER)


async def _exec(tenant: uuid.UUID, sql: str, params: dict[str, object] | None = None, *, org: OrgScope | None = None) -> int:
    async with tenant_session(tenant, org=org) as s:
        result = await s.execute(text(sql), params or {})
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _doc(tenant: uuid.UUID, status: str) -> uuid.UUID:
    doc_id = uuid.uuid4()
    await _exec(tenant, f"INSERT INTO {DOCS} (id, tenant_id, status, title) VALUES (:id, :t, :s, 'x')", {"id": doc_id, "t": tenant, "s": status})
    return doc_id


async def _refused(tenant: uuid.UUID, sql: str, params: dict[str, object], *, org: OrgScope | None = None) -> None:
    with pytest.raises(DBAPIError) as exc:
        await _exec(tenant, sql, params, org=org)
    assert sqlstate(exc.value) == "TOS02"
    domain = translate(exc.value)
    assert domain is not None and domain.code == "document_immutable" and domain.status_code == 409


async def test_mutable_states_allow_edits_and_deletes(tables: None) -> None:
    tenant = uuid.uuid4()
    for status in ("draft", "pending_approval"):
        doc = await _doc(tenant, status)
        assert await _exec(tenant, f"UPDATE {DOCS} SET title = 'edited' WHERE id = :id", {"id": doc}) == 1
    doc = await _doc(tenant, "pending_approval")
    assert await _exec(tenant, f"UPDATE {DOCS} SET status = 'issued' WHERE id = :id", {"id": doc}) == 1
    draft = await _doc(tenant, "draft")
    assert await _exec(tenant, f"DELETE FROM {DOCS} WHERE id = :id", {"id": draft}) == 1


async def test_issued_rows_are_immutable(tables: None) -> None:
    tenant = uuid.uuid4()
    doc = await _doc(tenant, "issued")
    await _refused(tenant, f"UPDATE {DOCS} SET title = 'tampered' WHERE id = :id", {"id": doc})
    await _refused(tenant, f"UPDATE {DOCS} SET status = 'draft' WHERE id = :id", {"id": doc})
    await _refused(tenant, f"DELETE FROM {DOCS} WHERE id = :id", {"id": doc})
    # Allow-listed operational column and updated_at may change.
    assert await _exec(tenant, f"UPDATE {DOCS} SET note = 'emailed', updated_at = now() WHERE id = :id", {"id": doc}) == 1


async def test_allowed_transition_with_its_columns(tables: None) -> None:
    tenant = uuid.uuid4()
    doc = await _doc(tenant, "issued")
    # The transition may carry its columns, but not other content.
    await _refused(
        tenant, f"UPDATE {DOCS} SET status = 'cancelled', cancel_basis = 'x', title = 'y' WHERE id = :id", {"id": doc}
    )
    assert await _exec(tenant, f"UPDATE {DOCS} SET status = 'cancelled', cancel_basis = 'irn' WHERE id = :id", {"id": doc}) == 1
    await _refused(tenant, f"UPDATE {DOCS} SET status = 'issued' WHERE id = :id", {"id": doc})


async def test_child_rows_follow_the_parent(tables: None) -> None:
    tenant = uuid.uuid4()
    draft = await _doc(tenant, "draft")
    line = uuid.uuid4()
    await _exec(tenant, f"INSERT INTO {LINES} (id, tenant_id, doc_id, amount) VALUES (:id, :t, :d, 10)", {"id": line, "t": tenant, "d": draft})
    assert await _exec(tenant, f"UPDATE {LINES} SET amount = 11 WHERE id = :id", {"id": line}) == 1
    await _exec(tenant, f"UPDATE {DOCS} SET status = 'issued' WHERE id = :id", {"id": draft})
    await _refused(tenant, f"UPDATE {LINES} SET amount = 12 WHERE id = :id", {"id": line})
    await _refused(tenant, f"DELETE FROM {LINES} WHERE id = :id", {"id": line})
    await _refused(
        tenant,
        f"INSERT INTO {LINES} (tenant_id, doc_id, amount) VALUES (:t, :d, 1)",
        {"t": tenant, "d": draft},
    )
    assert await _exec(tenant, f"UPDATE {LINES} SET note = 'ok' WHERE id = :id", {"id": line}) == 1
    # Moving a line from a draft into an issued document is refused too.
    other = await _doc(tenant, "draft")
    line2 = uuid.uuid4()
    await _exec(tenant, f"INSERT INTO {LINES} (id, tenant_id, doc_id, amount) VALUES (:id, :t, :d, 1)", {"id": line2, "t": tenant, "d": other})
    await _refused(tenant, f"UPDATE {LINES} SET doc_id = :d WHERE id = :id", {"id": line2, "d": draft})


async def test_status_less_tables_only_allow_listed_columns(tables: None) -> None:
    tenant = uuid.uuid4()
    row = uuid.uuid4()
    await _exec(tenant, f"INSERT INTO {LEDGER} (id, tenant_id, state, amount) VALUES (:id, :t, 'reserved', 5)", {"id": row, "t": tenant})
    assert await _exec(tenant, f"UPDATE {LEDGER} SET state = 'posted' WHERE id = :id", {"id": row}) == 1
    await _refused(tenant, f"UPDATE {LEDGER} SET amount = 6 WHERE id = :id", {"id": row})
    await _refused(tenant, f"DELETE FROM {LEDGER} WHERE id = :id", {"id": row})
    # An external org scope does not bypass the guard.
    await _refused(tenant, f"UPDATE {LEDGER} SET amount = 7 WHERE id = :id", {"id": row}, org=OrgScope.of("worker", uuid.uuid4()))


async def test_helpers_are_idempotent_and_reversible(owner_db: AsyncEngine, tables: None) -> None:
    async with owner_db.begin() as conn:
        for stmt in enable_immutability_sql(LEDGER, status_column=None, allowed_columns=("state", "amount")):
            await conn.execute(text(stmt))
    tenant = uuid.uuid4()
    row = uuid.uuid4()
    await _exec(tenant, f"INSERT INTO {LEDGER} (id, tenant_id, state, amount) VALUES (:id, :t, 'reserved', 5)", {"id": row, "t": tenant})
    assert await _exec(tenant, f"UPDATE {LEDGER} SET amount = 6 WHERE id = :id", {"id": row}) == 1
    async with owner_db.begin() as conn:
        for stmt in disable_immutability_sql(LEDGER):
            await conn.execute(text(stmt))
    assert await _exec(tenant, f"DELETE FROM {LEDGER} WHERE id = :id", {"id": row}) == 1
    async with owner_db.begin() as conn:
        for stmt in enable_immutability_sql(LEDGER, status_column=None, allowed_columns=("state",)):
            await conn.execute(text(stmt))
