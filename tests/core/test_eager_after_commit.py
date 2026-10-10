"""Epic 13 (deferred-work fix): with CELERY_TASK_ALWAYS_EAGER, a task enqueued by an after-commit handler runs
after ``commit()`` has returned (never inside the commit), sees the committed rows, and needs no running-loop
workaround from the caller."""

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.core.events import DISPATCHING, emit, subscribe, unsubscribe
from app.tasks.runtime import enqueue, run
from tests.factories import create_tenant_row

QUEUE = "interactive"
_results: list[tuple[str, int | None]] = []


async def _read(tenant_id: uuid.UUID, key: str) -> int | None:
    async with tenant_session(tenant_id) as s:
        value = (await s.execute(text("SELECT value FROM tenant_sequences WHERE key = :k"), {"k": key})).scalar_one_or_none()
        return None if value is None else int(value)


@celery_app.task(name="tests.eager_after_commit_probe")  # type: ignore[misc,untyped-decorator,unused-ignore]
def probe(tenant_id: str, key: str) -> None:
    tid = uuid.UUID(tenant_id)
    _results.append((key, run(lambda: _read(tid, key), tenant_id=tid)))


@dataclass(frozen=True)
class RowWritten:
    tenant_id: uuid.UUID
    key: str


async def test_handler_task_runs_after_commit_with_committed_data() -> None:
    assert celery_app.conf.task_always_eager
    _results.clear()
    observed: list[object] = []

    def handler(event: RowWritten) -> None:
        enqueue(probe, str(event.tenant_id), event.key)
        observed.append(DISPATCHING.get() is not None)
        observed.append(list(_results))  # the task has NOT run inside the commit

    subscribe(RowWritten, handler)
    try:
        tenant_id = await create_tenant_row()
        key = f"probe-{uuid.uuid4().hex[:8]}"
        async with tenant_session(tenant_id) as s:
            await s.execute(
                text("INSERT INTO tenant_sequences (tenant_id, key, value) VALUES (:t, :k, 42)"),
                {"t": tenant_id, "k": key},
            )
            emit(s, RowWritten(tenant_id, key))
        assert observed == [True, []]
        assert _results == [(key, 42)]
    finally:
        unsubscribe(RowWritten, handler)


async def test_explicit_commit_flushes_deferred_tasks_immediately() -> None:
    _results.clear()

    def handler(event: RowWritten) -> None:
        enqueue(probe, str(event.tenant_id), event.key)

    subscribe(RowWritten, handler)
    try:
        tenant_id = await create_tenant_row()
        key = f"probe-{uuid.uuid4().hex[:8]}"
        async with tenant_session(tenant_id) as s:
            await s.execute(
                text("INSERT INTO tenant_sequences (tenant_id, key, value) VALUES (:t, :k, 7)"),
                {"t": tenant_id, "k": key},
            )
            emit(s, RowWritten(tenant_id, key))
            await s.commit()
            assert _results == [(key, 7)]
    finally:
        unsubscribe(RowWritten, handler)
