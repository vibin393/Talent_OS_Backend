# ruff: noqa: E501, N818
"""Epic 66 (AD-79): ``subscribe_in_tx`` handlers run inside the committing transaction (``before_commit``).

* rows added by a handler commit with the transaction; a rollback writes nothing;
* a rolled-back savepoint drops its events (no row); a released savepoint's events are written at the outer commit;
* the handler runs once per event, only at the outermost commit;
* a handler error aborts the commit (the domain change is not committed either).
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import String, Uuid, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.database import tenant_session
from app.core.events import emit, subscribe_in_tx, unsubscribe_in_tx


class _Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class InTxEvent:
    tenant_id: uuid.UUID
    name: str


class _Boom(Exception):
    pass


class _Row(_Base):
    __tablename__ = (
        "test_rls_items"  # tests/conftest.py RLS_TEST_TABLE (created by the ``rls_table`` fixture)
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    name: Mapped[str] = mapped_column(String)


@pytest.fixture
def rls_row_cls(rls_table: str) -> type:
    assert rls_table == _Row.__tablename__
    return _Row


@pytest.fixture
def calls(rls_row_cls: type) -> Iterator[list[str]]:
    seen: list[str] = []

    def handler(session: Session, event: InTxEvent) -> None:
        seen.append(event.name)
        if event.name == "explode":
            raise _Boom
        session.add(rls_row_cls(tenant_id=event.tenant_id, name=f"outbox:{event.name}"))

    subscribe_in_tx(InTxEvent, handler)
    yield seen
    unsubscribe_in_tx(InTxEvent, handler)


async def _names(rls_table: str, tenant: uuid.UUID) -> list[str]:
    async with tenant_session(tenant) as s:
        return sorted((await s.execute(text(f"SELECT name FROM {rls_table}"))).scalars())


async def test_rows_commit_with_the_transaction(rls_table: str, calls: list[str]) -> None:
    tenant = uuid.uuid4()
    async with tenant_session(tenant) as s:
        await s.execute(
            text(f"INSERT INTO {rls_table} (tenant_id, name) VALUES (:t, 'domain')"), {"t": tenant}
        )
        emit(s, InTxEvent(tenant, "a"))
        emit(s, InTxEvent(tenant, "b"))
    assert calls == ["a", "b"]
    assert await _names(rls_table, tenant) == ["domain", "outbox:a", "outbox:b"]


async def test_rollback_writes_nothing(rls_table: str, calls: list[str]) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(_Boom):
        async with tenant_session(tenant) as s:
            emit(s, InTxEvent(tenant, "x"))
            raise _Boom
    assert calls == []
    assert await _names(rls_table, tenant) == []


async def test_savepoints(rls_table: str, calls: list[str]) -> None:
    tenant = uuid.uuid4()
    async with tenant_session(tenant) as s:
        with pytest.raises(_Boom):
            async with s.begin_nested():
                emit(s, InTxEvent(tenant, "dropped"))
                raise _Boom
        async with s.begin_nested():
            emit(s, InTxEvent(tenant, "released"))
        assert calls == []  # never at a savepoint release
        emit(s, InTxEvent(tenant, "outer"))
    assert calls == ["released", "outer"]
    assert await _names(rls_table, tenant) == ["outbox:outer", "outbox:released"]


async def test_handler_error_aborts_the_commit(rls_table: str, calls: list[str]) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(_Boom):
        async with tenant_session(tenant) as s:
            await s.execute(
                text(f"INSERT INTO {rls_table} (tenant_id, name) VALUES (:t, 'domain')"), {"t": tenant}
            )
            emit(s, InTxEvent(tenant, "explode"))
    assert await _names(rls_table, tenant) == []


async def test_no_handler_no_effect(rls_table: str) -> None:
    tenant = uuid.uuid4()
    async with tenant_session(tenant) as s:
        emit(s, InTxEvent(tenant, "unhandled"))
    assert await _names(rls_table, tenant) == []
