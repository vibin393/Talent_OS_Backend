"""AD-37: every registered ``app.tasks.*`` task declares a known queue; routing follows the declaration."""

import importlib

from app.core.celery import celery_app, queue_for, route_task
from app.core.constants import CEL_DEFAULT_QUEUE, CEL_TASK_PATHS, QUEUES


def _app_tasks() -> dict[str, object]:
    for path in CEL_TASK_PATHS:
        importlib.import_module(path)
    celery_app.loader.import_default_modules()
    return {name: task for name, task in celery_app.tasks.items() if task.__module__.startswith("app.tasks.")}


def test_every_task_declares_a_known_queue() -> None:
    tasks = _app_tasks()
    assert len(tasks) > 50
    undeclared = sorted(name for name, task in tasks.items() if queue_for(task) is None)
    unknown = sorted(name for name, task in tasks.items() if queue_for(task) not in (None, *QUEUES))
    assert undeclared == [], f"tasks without QUEUE / queue=: {undeclared}"
    assert unknown == [], f"tasks on unknown queues: {unknown}"


def test_routing_by_declaration() -> None:
    _app_tasks()
    expected = {
        "connectors.run_job": "connectors",
        "legal_entities.ensure_periods_all": "maintenance",
        "maintenance.purge_idempotency_keys": "maintenance",
        "privacy.retention_tick": "maintenance",
        "notifications.fan_out": "interactive",
    }
    for name, queue in expected.items():
        assert route_task(name, (), {}, {}, task=celery_app.tasks[name]) == {"queue": queue}
    ai_tasks = [n for n, t in celery_app.tasks.items() if t.__module__ == "app.tasks.ai"]
    assert ai_tasks and all(queue_for(celery_app.tasks[n]) == "ai" for n in ai_tasks)
    assert CEL_DEFAULT_QUEUE == "interactive"
    assert celery_app.conf.task_default_queue == "interactive"


def test_unknown_task_routes_to_default() -> None:
    assert route_task("nope.unknown", (), {}, {}) == {"queue": CEL_DEFAULT_QUEUE}


def test_phase3_queues() -> None:
    """Epic 36 (AD-54) + Epic 53 (AD-68): the ten queues, in order; P3 pool refresh / graph cache / immigration alerts
    use maintenance; Phase 4 adds ``agents`` and ``conversations``."""
    assert QUEUES == (
        "interactive",
        "finance_runs",
        "connectors",
        "ai",
        "maintenance",
        "campaigns",
        "analytics",
        "reports",
        "agents",
        "conversations",
        "webhooks",
        "ecosystem",
        "telephony",
    )
