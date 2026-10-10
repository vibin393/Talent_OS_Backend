# ruff: noqa: E501
"""Campaign copy wiring (Epic 78), autodiscovered by ``core.hooks``.

* notification types ``campaign_copy_ready`` (requester + ``campaign:copy_approve`` holders; in-app + email) and
  ``campaign_copy_failed`` (requester);
* settings hub entry ``marketing`` / ``campaign_copy`` "AI campaign copy" (the P4 agent settings endpoint);
* the P3 re-approval guards (``register_submit_guard`` / ``register_step_activation_guard``);
* ``AgentRunFinished`` / ``AgentRunFailed`` of a ``campaign_copy`` run -> ``campaign_copy.finalize_run`` (queue
  ``agents``);
* audit action labels.
"""

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.services.campaign_copy import guards
from app.services.campaign_copy.common import AGENT_KEY


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def nobody(db: AsyncSession, payload: Any) -> set[Any]:  # noqa: ARG001 - delivered with explicit recipients
        return set()

    async def render(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        return ns.Content(title=str(payload.get("title") or "AI campaign copy"), link_path=payload.get("link_path"))

    defs = {
        "campaign_copy_ready": ns.NotificationType("Campaign copy ready for approval", "Marketing", "both", False, nobody, render, internal_only=True),
        "campaign_copy_failed": ns.NotificationType("Campaign copy could not be drafted", "Marketing", "in_app", False, nobody, render, internal_only=True),
    }  # fmt: skip
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _settings_entries() -> None:
    from app.services import settings_service

    settings_service.register_settings_entry(
        "marketing", "campaign_copy", "AI campaign copy",
        "Turn on AI-drafted campaign and sequence copy, its monthly token budget and who approves it.",
        f"/api/v1/agents/{AGENT_KEY}", "ai:agent_configure", owner_epic="78",
    )  # fmt: skip


def _run_outcomes() -> None:
    from app.events.agents import AgentRunFailed, AgentRunFinished

    def handle(event: Any) -> None:
        if getattr(event, "agent_key", None) != AGENT_KEY:
            return
        try:
            from app.tasks import campaign_copy as tasks
            from app.tasks.runtime import enqueue
        except ImportError:  # pragma: no cover
            return
        status = getattr(event, "status", None) or "failed"
        enqueue(tasks.finalize_run, str(event.tenant_id), str(event.run_id), status, getattr(event, "reason_code", None))

    handle.__name__ = "campaign_copy_run_outcome"
    subscribe(AgentRunFinished, handle)
    subscribe(AgentRunFailed, handle)


def _audit_labels() -> None:
    try:
        from app.services import audit_timeline_sources as sources
    except ImportError:  # pragma: no cover
        return
    register = getattr(sources, "register_action_label", None)
    if register is None:
        return
    for action, label in (
        ("campaign_copy.requested", "AI campaign copy requested"),
        ("campaign_copy.cancelled", "AI campaign copy request cancelled"),
        ("campaign_copy.edited", "AI campaign copy edited"),
        ("campaign_copy.approved", "AI campaign copy approved"),
        ("campaign_copy.used", "AI campaign copy used"),
        ("campaign_copy.discarded", "AI campaign copy discarded"),
    ):
        try:
            register(action, label)
        except Exception:
            logger.debug("Audit label {} not registered", action)


_notifications()
_settings_entries()
guards.register()
_run_outcomes()
_audit_labels()
