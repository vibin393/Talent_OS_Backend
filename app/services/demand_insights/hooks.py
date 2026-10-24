# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Demand insights wiring (Epic 55), autodiscovered by ``core.hooks``: the notification types
``demand_duplicate_flagged`` (in-app) and ``demand_sla_digest`` (in-app + email digest, default on) and the workflow
trigger ``demand.duplicate_flagged`` (entity ``demand``) through the P2 ``register_trigger``."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def nobody(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument] - explicit recipients  # NOSONAR
        return set()

    async def render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title=str(payload.get("title") or "Demand Agent"), link_path=payload.get("link_path")
        )

    defs = {
        "demand_duplicate_flagged": ns.NotificationType("Possible duplicate demand", "Demands", "in_app", False, nobody, render, internal_only=True),
        "demand_sla_digest": ns.NotificationType("Daily SLA risk digest", "Demands", "both", False, nobody, render, internal_only=True),
    }  # fmt: skip
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _workflow_trigger() -> None:
    try:
        from app.services.workflow.registry import TRIGGERS, FieldDef, Trigger, register_trigger
    except ImportError:  # pragma: no cover
        return
    key = "demand.duplicate_flagged"
    if key in TRIGGERS:
        return
    register_trigger(
        key,
        Trigger(
            key, "Possible duplicate demand flagged", "app.events.demand_intake", "DemandDuplicateFlagged", "demand",
            "demand_id", {"score": ("score", FieldDef("str", "Similarity score"))},
        ),
    )  # fmt: skip


def _sla_trigger() -> None:
    """The Demand Agent's ``sla_changed`` trigger (kind ``inbound``): only ``at_risk`` / ``breached`` SLA changes start
    an ``sla_check`` run (dedup ``sla:{demand}:{milestone}:{status}``) -- the foundation event wiring has no filter."""
    from app.core.events import subscribe
    from app.events.demands import DemandSlaStatusChanged

    def handle(event: Any) -> None:
        if getattr(event, "status", None) not in ("at_risk", "breached"):
            return
        try:
            from app.tasks import agents as agent_tasks
            from app.tasks.runtime import enqueue
        except ImportError:  # pragma: no cover
            return
        enqueue(
            agent_tasks.start_from_event, str(event.tenant_id), "demand", "sla_changed", "demand", str(event.demand_id),
            f"sla:{event.demand_id}:{event.milestone}:{event.status}",
        )  # fmt: skip

    handle.__name__ = "demand_agent_sla_changed"
    subscribe(DemandSlaStatusChanged, handle)


_notifications()
_workflow_trigger()
_sla_trigger()
