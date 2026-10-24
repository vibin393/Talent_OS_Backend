# ruff: file-ignore[line-too-long]
"""Demand intake wiring (Epic 55), autodiscovered by ``core.hooks``.

* notification types ``demand_intake_unmatched`` (fallback owner, with the Task) and ``demand_drafts_created``
  (the Accountable Owner, in-app + email);
* settings hub entries "Demand intake" (``settings:manage``) and "Demand Agent" (``ai:agent_configure``);
* the workflow trigger ``demand_intake.received`` (entity ``client``; matched messages only) through the P2
  ``register_trigger`` (no ``catalogue_p4.py`` edit);
* the run outcome write-back: ``AgentRunFinished`` / ``AgentRunFailed`` of a ``demand`` run on a
  ``demand_intake_message`` subject enqueue ``demand_intake.finalize_run``;
* audit action labels.
"""

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def nobody(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument] - delivered with explicit recipients  # NOSONAR
        return set()

    async def render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title=str(payload.get("title") or "Demand Agent"), link_path=payload.get("link_path")
        )

    defs = {
        "demand_intake_unmatched": ns.NotificationType("Intake email not matched", "Demands", "in_app", False, nobody, render, internal_only=True),
        "demand_drafts_created": ns.NotificationType("Demand Agent drafted demands", "Demands", "both", False, nobody, render, internal_only=True),
    }  # fmt: skip
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _settings_entries() -> None:
    from app.services import settings_service

    settings_service.register_settings_entry(
        "demands", "demand_intake", "Demand intake", "Intake mailbox, verified sender domains and fallback owner",
        "/api/v1/demands/intake-settings", "settings:manage", owner_epic="55",
    )  # fmt: skip
    settings_service.register_settings_entry(
        "ai", "demand_agent", "Demand Agent", "Duplicate and SLA-risk thresholds, JD template",
        "/api/v1/demands/agent-settings", "ai:agent_configure", owner_epic="55",
    )  # fmt: skip


def _workflow_trigger() -> None:
    try:
        from app.services.workflow.registry import TRIGGERS, FieldDef, Trigger, register_trigger
    except ImportError:  # pragma: no cover - workflow absent
        return
    key = "demand_intake.received"
    if key in TRIGGERS:
        return
    register_trigger(
        key,
        Trigger(
            key, "Client requirement email received", "app.events.demand_intake", "DemandIntakeMatched", "client",
            "matched_client_id",
            {"source": ("source", FieldDef("enum", "Source", ("platform_address", "connected_mailbox", "upload", "paste")))},
            matches=lambda e: getattr(e, "matched_client_id", None) is not None,
        ),
    )  # fmt: skip


def _run_outcomes() -> None:
    from app.events.agents import AgentRunFailed, AgentRunFinished

    def handle(event: Any) -> None:
        if (
            getattr(event, "agent_key", None) != "demand"
            or getattr(event, "subject_type", None) != "demand_intake_message"
        ):
            return
        if event.subject_id is None:
            return
        try:
            from app.tasks import demand_intake as tasks
            from app.tasks.runtime import enqueue
        except ImportError:  # pragma: no cover
            return
        status = getattr(event, "status", None) or "failed"
        reason = getattr(event, "reason_code", None)
        enqueue(
            tasks.finalize_run, str(event.tenant_id), str(event.subject_id), str(event.run_id), status, reason
        )

    handle.__name__ = "demand_intake_run_outcome"
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
        ("demand_intake.webhook_rejected", "Intake webhook call rejected"),
        ("demand_intake.address_rotated", "Intake address regenerated"),
        ("demand_insights.flag_resolved", "Demand flag resolved"),
    ):
        try:
            register(action, label)
        except Exception:  # pragma: no cover - label registry is best effort
            logger.debug("Audit label {} not registered", action)


_notifications()
_settings_entries()
_workflow_trigger()
_run_outcomes()
_audit_labels()
