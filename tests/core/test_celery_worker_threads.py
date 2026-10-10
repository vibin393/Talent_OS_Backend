"""Regression: async Celery tasks under ``celery worker --pool=threads`` (and solo / prefork).

The SLA beat job crashed in the worker with ``RuntimeError: no running event loop`` followed by
``AttributeError("'NoneType' object has no attribute 'send'")``: ``demands.tasks.evaluate_slas_all`` ran
``directory.active_tenant_ids`` in a fresh ``asyncio.run`` loop on the *process-wide* pooled engine, whose
asyncpg connections belonged to the loop (thread) that first opened them. Every session must now come from
an engine bound to the loop that is running it.
"""

import asyncio
import importlib
import threading
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from celery._state import _set_task_join_will_block
from sqlalchemy import text

from app.core import database
from app.core.celery import celery_app
from app.core.constants import CEL_TASK_PATHS
from app.job.schedules import BEAT_SCHEDULE
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai  # noqa: F401
from tests.modules.demands.helpers import DOrg, dorg  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_celery_join_flag() -> Iterator[None]:
    """Reset Celery's process-global "inside a task" flag after the test.

    Eager ``.delay`` saves/restores it; eager fan-outs racing on several threads can leave it stuck ``True``
    and break later ``.delay().get()`` calls.
    """
    yield
    _set_task_join_will_block(False)


def _in_thread(fn: Any) -> Any:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def _platform_ping() -> int:
    async with database.platform_session() as db:
        return int((await db.execute(text("SELECT 1"))).scalar_one())


def test_sessions_work_from_successive_loops_in_different_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh process: the first loop that uses the pool binds it; later loops (other threads / after the
    # first loop closed) must never borrow its connections.
    monkeypatch.setattr(database, "_app_engine", None)
    monkeypatch.setattr(database, "_app_sessionmaker", None)
    monkeypatch.setattr(database, "_app_engine_loop", None)
    try:
        for _ in range(3):
            assert _in_thread(lambda: asyncio.run(_platform_ping())) == 1
    finally:
        engine = database._app_engine
        if engine is not None:
            engine.sync_engine.dispose(close=False)  # its loop is closed; just drop the pool


def _run_task(name: str) -> str | None:
    try:
        result = celery_app.tasks[name].apply()
        if result.failed():
            return f"{name}: {result.traceback or result.result!r}"
    except Exception:
        return f"{name}: {traceback.format_exc()}"
    return None


@pytest.fixture
def only_this_tenant(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> DOrg:  # noqa: F811 - fixture import
    """Limit fan-outs to this test's tenant (the shared test DB accumulates thousands of tenants).

    The tenant listing still runs its real query in the task's own loop.
    """
    from app.services import directory_service, identity_bridge

    real_list, real_active = identity_bridge.list_tenants, directory_service.active_tenant_ids

    async def list_tenants(db: Any, *args: Any, **kwargs: Any) -> Any:
        return [t for t in await real_list(db, *args, **kwargs) if t.id == dorg.tenant_id]

    async def active_tenant_ids() -> list[Any]:
        return [t for t in await real_active() if t == dorg.tenant_id]

    monkeypatch.setattr(identity_bridge, "list_tenants", list_tenants)
    monkeypatch.setattr(directory_service, "active_tenant_ids", active_tenant_ids)
    return dorg


def test_every_beat_task_runs_in_worker_threads(only_this_tenant: DOrg) -> None:
    """Run every beat task twice through the real Celery app on a 4-thread pool (``--pool=threads
    --concurrency=4``) while the test loop holds the process-wide pool -- none may fail."""
    for path in CEL_TASK_PATHS:
        importlib.import_module(path)
    names = sorted({str(entry["task"]) for entry in BEAT_SCHEDULE.values()})
    missing = [n for n in names if n not in celery_app.tasks]
    assert not missing, f"beat tasks not registered: {missing}"
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery-worker") as pool:
        errors = [e for e in pool.map(_run_task, names * 2) if e is not None]
    assert not errors, "\n\n".join(errors)


def test_sla_beat_task_counts_active_tenants(only_this_tenant: DOrg) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = celery_app.tasks["demands.tasks.evaluate_slas_all"]
        results = [f.result() for f in [pool.submit(task.apply) for _ in range(4)]]
    assert all(r.successful() for r in results), [r.traceback for r in results]
    assert [r.result for r in results] == [1, 1, 1, 1]
