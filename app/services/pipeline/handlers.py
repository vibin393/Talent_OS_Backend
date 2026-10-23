"""After-commit event handlers of the pipeline: enqueue the Celery tasks (AD-9). Never log PII."""

from loguru import logger

from app.core.events import subscribe
from app.events.pipeline import (
    AIScreeningAnswered,
    AIScreeningSent,
    AIScreeningSummaryRequested,
    RejectionEmailRequested,
    SubmissionSent,
)

_registered = False


def _enqueue(task_name: str, *args: str) -> None:
    from app.core.celery import celery_app

    try:
        celery_app.tasks[task_name].delay(*args)
    except Exception:
        logger.exception("Could not enqueue {} ({})", task_name, args[0] if args else "")


def _ensure_tasks_loaded() -> None:
    import app.tasks.pipeline_ai  # ruff: ignore[unused-import]


def _on_ai_sent(e: AIScreeningSent) -> None:
    _ensure_tasks_loaded()
    _enqueue("pipeline.tasks.deliver_ai_screening_invite", str(e.tenant_id), str(e.screening_id))


def _on_answered(e: AIScreeningAnswered | AIScreeningSummaryRequested) -> None:
    _ensure_tasks_loaded()
    _enqueue("pipeline_ai.tasks.summarize_ai_screening", str(e.tenant_id), str(e.screening_id))


def _on_submission_sent(e: SubmissionSent) -> None:
    _ensure_tasks_loaded()
    _enqueue("pipeline.tasks.send_submission_emails", str(e.tenant_id), str(e.submission_id))


def _on_rejection(e: RejectionEmailRequested) -> None:
    _ensure_tasks_loaded()
    _enqueue("pipeline.tasks.send_rejection_email", str(e.tenant_id), str(e.application_id))


def _on_tenant(e: object) -> None:
    _ensure_tasks_loaded()
    _enqueue("pipeline.tasks.ensure_defaults", str(getattr(e, "tenant_id", "")))


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    subscribe(AIScreeningSent, _on_ai_sent)
    subscribe(AIScreeningAnswered, _on_answered)
    subscribe(AIScreeningSummaryRequested, _on_answered)
    subscribe(SubmissionSent, _on_submission_sent)
    subscribe(RejectionEmailRequested, _on_rejection)
    try:
        from app.events.identity import TenantProvisioned
    except ImportError:  # pragma: no cover
        pass
    else:
        subscribe(TenantProvisioned, _on_tenant)
    _registered = True
