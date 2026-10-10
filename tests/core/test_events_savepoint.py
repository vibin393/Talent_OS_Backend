"""Epic 13 (deferred-work fix): domain events emitted inside a rolled-back savepoint are dropped; events of a
released savepoint are dispatched with the outer commit."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.events import emit, subscribe, unsubscribe


@dataclass(frozen=True)
class SavepointEvent:
    name: str


@pytest.fixture
def seen() -> Iterator[list[str]]:
    names: list[str] = []

    def handler(event: SavepointEvent) -> None:
        names.append(event.name)

    subscribe(SavepointEvent, handler)
    yield names
    unsubscribe(SavepointEvent, handler)


class _Boom(Exception):
    pass


async def test_event_before_savepoint_survives_its_rollback(seen: list[str]) -> None:
    async with tenant_session(uuid.uuid4()) as s:
        emit(s, SavepointEvent("before"))
        with pytest.raises(_Boom):
            async with s.begin_nested():
                await s.execute(text("SELECT 1"))
                raise _Boom
    assert seen == ["before"]


async def test_event_inside_rolled_back_savepoint_is_dropped(seen: list[str]) -> None:
    async with tenant_session(uuid.uuid4()) as s:
        with pytest.raises(_Boom):
            async with s.begin_nested():
                emit(s, SavepointEvent("inside"))
                raise _Boom
        emit(s, SavepointEvent("after"))
    assert seen == ["after"]


async def test_nested_savepoints(seen: list[str]) -> None:
    async with tenant_session(uuid.uuid4()) as s:
        with pytest.raises(_Boom):
            async with s.begin_nested():
                emit(s, SavepointEvent("outer-sp"))
                async with s.begin_nested():  # released into the outer savepoint
                    emit(s, SavepointEvent("inner-released"))
                raise _Boom  # rolls back the outer savepoint -> both dropped
        async with s.begin_nested():
            emit(s, SavepointEvent("kept"))
    assert seen == ["kept"]


async def test_event_inside_released_savepoint_dispatched_on_commit(seen: list[str]) -> None:
    async with tenant_session(uuid.uuid4()) as s:
        async with s.begin_nested():
            emit(s, SavepointEvent("released"))
        assert seen == []  # not before the outer commit
    assert seen == ["released"]


async def test_outer_rollback_dispatches_nothing(seen: list[str]) -> None:
    with pytest.raises(_Boom):
        async with tenant_session(uuid.uuid4()) as s:
            emit(s, SavepointEvent("a"))
            async with s.begin_nested():
                emit(s, SavepointEvent("b"))
            raise _Boom
    assert seen == []
