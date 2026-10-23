# ruff: file-ignore[line-too-long]
"""Public API tasks (Epic 67, spec A5 / A4).

* ``public_api.start_import`` (queue ``interactive``) -- replay a public bulk import job (``public_api_service``).
* ``public_api.deprecation_report`` (queue ``maintenance``; beat daily, runs on ``public_api_deprecation_report_day``)
  -- last month's deprecated calls per Installation / API key into ``api_deprecation_notices`` + notifications.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.celery import celery_app
from app.tasks.runtime import run

QUEUE = "maintenance"
INTERACTIVE = "interactive"


@celery_app.task(name="public_api.start_import", queue=INTERACTIVE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def start_import(tenant_id: str, import_id: str) -> dict[str, int]:
    from app.services import public_api_service

    tid = uuid.UUID(tenant_id)
    return run(lambda: public_api_service.process_import(tid, uuid.UUID(import_id)), tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="public_api.deprecation_report", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def deprecation_report(force: bool = False) -> dict[str, Any]:
    from app.core.settings import get_settings
    from app.services import public_api_service

    if not force and datetime.now(UTC).day != get_settings().public_api_deprecation_report_day:
        return {"skipped": True}
    return run(public_api_service.deprecation_report_all)  # type: ignore[no-any-return]


__all__ = ["deprecation_report", "start_import"]
