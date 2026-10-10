"""Queued notifications of the job distribution domain (never block a sync or a request)."""

import uuid
from typing import Any


def fan_out(tenant_id: uuid.UUID, type_key: str, payload: dict[str, Any]) -> None:
    from loguru import logger

    from app.tasks import notifications as notification_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(notification_tasks.fan_out, str(tenant_id), type_key, payload)
    except Exception:  # pragma: no cover - logged
        logger.exception("Job board notification {} failed", type_key)
