# ruff: noqa: E501
"""Usage metering tasks (Epic 66, AD-78), queue ``maintenance``.

* ``metering.flush`` (beat every 60 s) -- Redis / buffer counters -> ``usage_counters``.
* ``metering.reconcile`` (daily 00:30 UTC) -- yesterday's metered ``api_request`` vs ``api_request_log`` (> 0.1 % alerts).
* ``api_request_log.purge`` (daily 02:50 UTC) -- 30-day retention of the partitioned request logs.
* ``partitions.ensure_monthly`` (daily 01:10 UTC) -- the current + 3 monthly partitions of every registered table.
"""

from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import run

QUEUE = "maintenance"


@celery_app.task(name="metering.flush", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def flush() -> dict[str, int]:
    from app.services import metering_service

    return run(metering_service.flush)  # type: ignore[no-any-return]


@celery_app.task(name="metering.reconcile", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def reconcile() -> dict[str, Any]:
    from app.services import metering_service

    async def job() -> dict[str, Any]:
        drifts = await metering_service.reconcile()
        return {
            tid: {"metered": d.metered, "logged": d.logged, "delta": d.delta, "alert": d.alert}
            for tid, d in drifts.items()
        }

    return run(job)  # type: ignore[no-any-return]


@celery_app.task(name="api_request_log.purge", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge_request_log() -> dict[str, Any]:
    from app.services import metering_service

    return run(metering_service.purge)  # type: ignore[no-any-return]


@celery_app.task(name="partitions.ensure_monthly", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def ensure_monthly() -> dict[str, list[str]]:
    from app.services import metering_service

    return run(metering_service.ensure_monthly)  # type: ignore[no-any-return]


__all__ = ["ensure_monthly", "flush", "purge_request_log", "reconcile"]
