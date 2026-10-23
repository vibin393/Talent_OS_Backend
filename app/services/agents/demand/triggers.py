# ruff: file-ignore[line-too-long]
"""Triggers of the ``demand`` agent (Epic 55, spec 0.1). Dedup keys: ``intake:{message_id}:{attempt}``,
``draft:{demand_id}``, ``opened:{demand_id}``, ``sla:{demand_id}:{milestone}:{status}``; schedule keys come from the
foundation (``sched:daily_sla_scan:<minute>``). ``on_active="drop"`` for all.

* ``intake_received`` -- event ``DemandIntakeMatched`` (the ``matched`` subclass of ``DemandIntakeReceived``: the
  foundation ``TriggerSpec`` has no filter, so the domain emits the narrower event); owner = the message's owner.
* ``draft_created`` -- P1 ``DemandCreated`` (every new demand is a draft).
* ``demand_opened`` -- P1 ``DemandOpened``.
* ``sla_changed`` -- kind ``inbound``: ``demand_insights`` hooks subscribe to P1 ``DemandSlaStatusChanged``, keep
  ``at_risk`` / ``breached`` only and start the run through the foundation's ``agents.start_from_event`` task (a
  filter the foundation event wiring cannot express).
* ``daily_sla_scan`` -- schedule ``30 2 * * *`` UTC (08:00 IST); owner = ``sla_scan_owner_id`` (none -> no run + a
  Task, FR-265).
"""

import uuid
from typing import Any

from app.core.agentic import TriggerSpec


async def intake_owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import demand_intake_service

    return await demand_intake_service.accountable_owner(ctx, db, subject)


async def demand_owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:  # ruff: ignore[unused-function-argument]
    if subject is None or subject[0] != "demand":
        return None
    from app.services import entity_refs

    return await entity_refs.entity_owner_id(db, "demand", subject[1])


async def scan_owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:  # ruff: ignore[unused-function-argument]
    from app.services import demand_intake_service

    return (await demand_intake_service.settings_row(db)).sla_scan_owner_id


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    """Default owner: the message owner (intake) or the demand owner."""
    if subject is not None and subject[0] == "demand_intake_message":
        return await intake_owner(ctx, db, subject)
    return await demand_owner(ctx, db, subject)


def _demand(e: Any) -> tuple[str, uuid.UUID]:
    return ("demand", e.demand_id)


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(
        key="intake_received", kind="event", event="DemandIntakeMatched", event_module="app.events.demand_intake",
        subject=lambda e: ("demand_intake_message", e.message_id),
        dedup_key=lambda e: f"intake:{e.message_id}:{e.attempt}", on_active="drop", owner=intake_owner,
    ),
    TriggerSpec(
        key="draft_created", kind="event", event="DemandCreated", event_module="app.events.demands",
        subject=_demand, dedup_key=lambda e: f"draft:{e.demand_id}", on_active="drop", owner=demand_owner,
    ),
    TriggerSpec(
        key="demand_opened", kind="event", event="DemandOpened", event_module="app.events.demands",
        subject=_demand, dedup_key=lambda e: f"opened:{e.demand_id}", on_active="drop", owner=demand_owner,
    ),
    TriggerSpec(key="sla_changed", kind="inbound", on_active="drop", owner=demand_owner),
    TriggerSpec(key="daily_sla_scan", kind="schedule", schedule="30 2 * * *", on_active="drop", owner=scan_owner),
    TriggerSpec(key="manual", kind="manual"),
)  # fmt: skip

SLA_STATUSES = frozenset({"at_risk", "breached"})


def sla_dedup_key(demand_id: uuid.UUID, milestone: str, status: str) -> str:
    return f"sla:{demand_id}:{milestone}:{status}"


__all__ = ["SLA_STATUSES", "TRIGGERS", "demand_owner", "intake_owner", "owner", "scan_owner", "sla_dedup_key"]
