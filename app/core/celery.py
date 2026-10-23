"""Celery application.

Broker/backend come from settings (REDIS_HOST / REDIS_PORT / REDIS_PASSWORD). Local dev may run tasks
eagerly (CELERY_TASK_ALWAYS_EAGER=true). Tasks take ``tenant_id`` as their first argument and do their DB
work inside ``app.core.database.tenant_session(tenant_id)`` (AD-9).

Epic 13 (AD-37): queues ``interactive`` (default), ``finance_runs``, ``connectors``, ``ai`` and ``maintenance``.
Every task declares its queue -- ``QUEUE = "<queue>"`` in its ``app/tasks/<domain>.py`` module or ``queue=`` on
the task -- and ``task_routes`` routes by that declaration (``queue_for``). Workers consume all queues:
``celery -A app.core.celery worker -Q interactive,finance_runs,connectors,ai,maintenance``. Worker and beat
processes import every ``app/services/**/hooks.py`` at start (``worker_init`` / ``beat_init``).
"""

import sys
from typing import Any

from celery import Celery
from celery.signals import beat_init, worker_init

from app.core.constants import CEL_DEFAULT_QUEUE, CEL_MAIN_NAME, CEL_TASK_PATHS, QUEUES
from app.core.settings import get_settings
from app.job.schedules import register_celery_schedules

_settings = get_settings()

celery_app = Celery(CEL_MAIN_NAME, broker=_settings.redis_url(0), backend=_settings.redis_url(1))


def queue_for(task: Any) -> str | None:
    """The queue a task declares: its own ``queue`` option, else its module's ``QUEUE`` (None = undeclared)."""
    explicit = getattr(task, "queue", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    module = sys.modules.get(getattr(task, "__module__", "") or "")
    declared = getattr(module, "QUEUE", None) if module is not None else None
    return declared if isinstance(declared, str) else None


def route_task(name: str, args: Any, kwargs: Any, options: Any, task: Any = None, **kw: Any) -> dict[str, str]:  # ruff: ignore[unused-function-argument]
    queue = queue_for(task) if task is not None else None
    if queue is None and name in celery_app.tasks:
        queue = queue_for(celery_app.tasks[name])
    return {"queue": queue if queue in QUEUES else CEL_DEFAULT_QUEUE}


celery_app.conf.update(
    task_default_queue=CEL_DEFAULT_QUEUE,
    task_routes=(route_task,),
    task_always_eager=_settings.celery_task_always_eager,
    task_eager_propagates=_settings.celery_task_always_eager,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

# Automatically discover and register tasks from specified package paths
celery_app.autodiscover_tasks(CEL_TASK_PATHS)

# Register scheduled tasks (periodic tasks) with the Celery application
register_celery_schedules(celery_app)


def _autodiscover_hooks(**_: Any) -> None:
    from app.core.hooks import autodiscover

    autodiscover("app.services")


worker_init.connect(_autodiscover_hooks, weak=False)
beat_init.connect(_autodiscover_hooks, weak=False)
