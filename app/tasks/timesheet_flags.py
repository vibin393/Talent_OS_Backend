# ruff: noqa: E501
"""Epic 63 Timesheet Agent tasks: ``timesheet_flags.evaluate`` / ``supersede`` (queue ``interactive``; deterministic
flags, AI on or off), ``timesheet_flags.start_flag_run`` and the hourly ``timesheet_flags.nudge_tick`` (``maintenance``).
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="timesheet_flags.evaluate", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def evaluate(tenant_id: str, timesheet_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.timesheet_flags import evaluate as ev

        async with tenant_session(tid) as db:
            return len(await ev.evaluate(db, uuid.UUID(timesheet_id)))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="timesheet_flags.supersede", queue="interactive", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def supersede(tenant_id: str, timesheet_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.timesheet_flags import evaluate as ev

        async with tenant_session(tid) as db:
            return await ev.supersede(db, uuid.UUID(timesheet_id))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="timesheet_flags.start_flag_run", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def start_flag_run(tenant_id: str, timesheet_id: str, version: int) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.timesheet_flags import ticks

        async with tenant_session(tid) as db:
            return await ticks.start_flag_run(db, uuid.UUID(timesheet_id), int(version))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="timesheet_flags.nudge_tick", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def nudge_tick() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.timesheet_flags import ticks

        async with tenant_session(tid) as db:
            return await ticks.nudge_tick(db)

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = ["evaluate", "nudge_tick", "start_flag_run", "supersede"]
