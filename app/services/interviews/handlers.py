"""After-commit handlers of the interview events: enqueue the invite task (AD-9). Never log PII."""

from loguru import logger

from app.core.events import subscribe
from app.events.interviews import InterviewCancelled, InterviewRescheduled, InterviewScheduled

_registered = False


def _enqueue(tenant_id: object, interview_id: object, action: str) -> None:
    from app.tasks import interviews as interview_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(interview_tasks.sync_interview_invites, str(tenant_id), str(interview_id), action)
    except Exception:
        logger.exception("Could not enqueue interview invites ({})", interview_id)


def _on_scheduled(e: InterviewScheduled) -> None:
    if e.send_invites:
        _enqueue(e.tenant_id, e.interview_id, "request")


def _on_rescheduled(e: InterviewRescheduled) -> None:
    if e.send_invites:
        _enqueue(e.tenant_id, e.interview_id, "request")


def _on_cancelled(e: InterviewCancelled) -> None:
    if e.send_invites:
        _enqueue(e.tenant_id, e.interview_id, "cancel")


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    subscribe(InterviewScheduled, _on_scheduled)
    subscribe(InterviewRescheduled, _on_rescheduled)
    subscribe(InterviewCancelled, _on_cancelled)
    _registered = True
