import uuid
from dataclasses import dataclass

import pytest

from app.core.database import tenant_session
from app.core.events import emit, subscribe, unsubscribe


@dataclass(frozen=True)
class ThingHappened:
    name: str


async def test_events_dispatch_only_after_commit() -> None:
    seen: list[str] = []

    def handler(event: ThingHappened) -> None:
        seen.append(event.name)

    subscribe(ThingHappened, handler)
    try:
        async with tenant_session(uuid.uuid4()) as s:
            emit(s, ThingHappened("committed"))
            assert seen == []
        assert seen == ["committed"]

        with pytest.raises(RuntimeError):
            async with tenant_session(uuid.uuid4()) as s:
                emit(s, ThingHappened("rolled-back"))
                raise RuntimeError("boom")
        assert seen == ["committed"]
    finally:
        unsubscribe(ThingHappened, handler)


async def test_failing_handler_does_not_block_others() -> None:
    seen: list[str] = []

    def bad(_: ThingHappened) -> None:
        raise ValueError("handler bug")

    def good(event: ThingHappened) -> None:
        seen.append(event.name)

    subscribe(ThingHappened, bad)
    subscribe(ThingHappened, good)
    try:
        async with tenant_session(uuid.uuid4()) as s:
            emit(s, ThingHappened("x"))
        assert seen == ["x"]
    finally:
        unsubscribe(ThingHappened, bad)
        unsubscribe(ThingHappened, good)
