# ruff: noqa: E501
"""MCP tasks (Epic 73, AD-92), queue ``maintenance``.

* ``mcp.end_sessions(tenant_id, filter, reason)`` -- after-commit kick from the kill switch / disable / uninstall /
  revocation handlers (spec 0.7);
* ``mcp.detector_rollup`` (beat, 5 min) -- mirror runs the runtime closed (idle, deadline) and auto-release holds;
* ``mcp.purge`` (daily) -- ``mcp_sessions`` metadata retention.
"""

import uuid
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


@celery_app.task(name="mcp.end_sessions", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def end_sessions(tenant_id: str, flt: dict[str, str | None], reason: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services import mcp_service

        return await mcp_service.end_sessions(
            tid, reason=reason, installation_id=_uuid(flt.get("installation_id")), user_id=_uuid(flt.get("user_id")),
            app_id=_uuid(flt.get("app_id")),
        )  # fmt: skip

    return int(run(job, tenant_id=tid) or 0)


@celery_app.task(name="mcp.detector_rollup", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def detector_rollup() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services import mcp_service

        return await mcp_service.rollup(tid)

    return run(lambda: for_each_tenant(job)) or {}  # type: ignore[no-any-return]


@celery_app.task(name="mcp.purge", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def purge() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services import mcp_service

        return await mcp_service.purge(tid)

    return run(lambda: for_each_tenant(job)) or {}  # type: ignore[no-any-return]
