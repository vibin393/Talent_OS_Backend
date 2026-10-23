# ruff: file-ignore[line-too-long]
"""Sourcing domain wiring (Epic 56), autodiscovered by ``core.hooks``.

* event handlers (after commit, enqueue a ``sourcing_agent.*`` task): ``MatchRunCompleted`` -> re-rank trigger,
  ``DemandStatusChanged`` away from ``open`` -> cancel active sourcing runs, ``ProposalDecided`` of
  ``outreach.send`` -> the entry's outreach status, ``AgentShortlistSaved`` -> notification
  ``sourcing_shortlist_ready``;
* the P3 marketing cap counter ``sourcing_outreach`` (coordinator decision C-9): approved agent outreach counts
  toward the 3-per-7-days frequency cap;
* the settings hub entry, audit action labels and the retention binding of ``agent_shortlist_entries``
  (class ``agent_logs``: drafts, notes and explanations are blanked, the decision facts stay).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe


# ---------------------------------------------------------------- events
def _subscribe() -> None:
    from app.events.agents import ProposalDecided
    from app.events.demands import DemandStatusChanged
    from app.events.matching import MatchRunCompleted
    from app.events.sourcing_agent import AgentShortlistSaved

    def on_match_run(e: MatchRunCompleted) -> None:
        from app.tasks import sourcing_agent as tasks
        from app.tasks.runtime import enqueue

        if e.status == "completed":
            enqueue(tasks.on_match_run, str(e.tenant_id), str(e.demand_id), str(e.run_id), e.status)

    def on_status(e: DemandStatusChanged) -> None:
        from app.tasks import sourcing_agent as tasks
        from app.tasks.runtime import enqueue

        if e.from_status == "open" and e.to_status != "open":
            enqueue(tasks.cancel_for_demand, str(e.tenant_id), str(e.demand_id))

    def on_decided(e: ProposalDecided) -> None:
        from app.tasks import sourcing_agent as tasks
        from app.tasks.runtime import enqueue

        if e.tool_key == "outreach.send":
            enqueue(tasks.on_proposal_decided, str(e.tenant_id), str(e.proposal_id), e.status)

    def on_saved(e: AgentShortlistSaved) -> None:
        from app.tasks import notifications as notification_tasks
        from app.tasks.runtime import enqueue

        enqueue(notification_tasks.fan_out, str(e.tenant_id), "sourcing_shortlist_ready", {
            "event_id": f"sourcing_shortlist:{e.shortlist_id}", "shortlist_id": str(e.shortlist_id),
            "demand_id": str(e.demand_id), "entry_count": e.entry_count, "new_count": e.new_count,
        })  # fmt: skip

    for fn, name in (
        (on_match_run, "match_run"),
        (on_status, "demand_status"),
        (on_decided, "proposal_decided"),
        (on_saved, "shortlist_saved"),
    ):
        fn.__name__ = f"sourcing_agent_on_{name}"
    subscribe(MatchRunCompleted, on_match_run)
    subscribe(DemandStatusChanged, on_status)
    subscribe(ProposalDecided, on_decided)
    subscribe(AgentShortlistSaved, on_saved)


# ---------------------------------------------------------------- cap counter (C-9)
async def outreach_counts(
    db: AsyncSession, candidate_ids: Sequence[uuid.UUID], since: datetime
) -> dict[uuid.UUID, int]:
    """Approved Sourcing Agent outreach sent to each candidate since ``since``."""
    from sqlalchemy import func, select

    from app.models.sourcing_agent import AgentShortlistEntry

    if not candidate_ids:
        return {}
    rows = await db.execute(
        select(AgentShortlistEntry.candidate_id, func.count())
        .where(
            AgentShortlistEntry.candidate_id.in_(list(candidate_ids)),
            AgentShortlistEntry.outreach_sent_at.is_not(None),
            AgentShortlistEntry.outreach_sent_at > since,
        )
        .group_by(AgentShortlistEntry.candidate_id)
    )
    return {cid: int(n) for cid, n in rows.all()}


def _register_cap_counter() -> None:
    from app.services.campaigns import governance

    governance.register_cap_counter("sourcing_outreach", outreach_counts)


# ---------------------------------------------------------------- notifications
def _register_notifications() -> None:
    from app.services import notifications_service as ns

    def _uuid(v: Any) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(v)) if v else None
        except ValueError:
            return None

    async def recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.services import entity_refs
        from app.services.demands import matching_port

        demand_id = _uuid(payload.get("demand_id"))
        if demand_id is None:
            return set()
        out: set[uuid.UUID] = set(await matching_port.get_assignees(db, demand_id))
        owner = await entity_refs.entity_owner_id(db, "demand", demand_id)
        if owner is not None:
            out.add(owner)
        return out

    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.services import entity_refs

        demand_id = _uuid(payload.get("demand_id"))
        label = await entity_refs.entity_label(db, "demand", demand_id) if demand_id else None
        n = int(payload.get("entry_count") or 0)
        new = int(payload.get("new_count") or 0)
        return ns.Content(
            title=f"Agent shortlist ready for {label or 'your Demand'}: {n} people"
            + (f" ({new} new)" if new else ""),
            entity_type="demand",
            entity_id=demand_id,
            link_path=f"/demands/{demand_id}?tab=agent_shortlist" if demand_id else None,
        )

    if "sourcing_shortlist_ready" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "sourcing_shortlist_ready",
            ns.NotificationType(
                "Agent shortlist ready", "AI agents", "in_app", False, recipients, render, internal_only=True
            ),
        )


# ---------------------------------------------------------------- settings / audit / retention
def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "sourcing_agent",
        "Sourcing Agent",
        "Shortlist size, outreach drafting",
        "/api/v1/settings/agents/sourcing",
        "ai:agent_configure",
        owner_epic="Epic 56",
    )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("sourcing.shortlist_saved", "The Sourcing Agent saved an agent shortlist"),
        ("sourcing.entry_added_to_pipeline", "An agent shortlist entry was added to the pipeline"),
        ("sourcing.entry_dismissed", "An agent shortlist entry was dismissed"),
        ("sourcing.entry_restored", "A dismissed agent shortlist entry was restored"),
        ("sourcing.outreach_sent", "Approved Sourcing Agent outreach was sent"),
        ("sourcing.settings_changed", "The Sourcing Agent settings changed"),
    ):
        audit.register_audit_action(key, text)


def _register_retention() -> None:
    from sqlalchemy import select, update

    from app.models.sourcing_agent import AgentShortlistEntry
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
        return (
            await db.execute(select(AgentShortlistEntry.candidate_id).where(AgentShortlistEntry.id == row_id))
        ).scalar_one_or_none()

    async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(AgentShortlistEntry.created_at).where(AgentShortlistEntry.id == row_id))
        ).scalar_one_or_none()

    async def _expire(
        db: AsyncSession,
        ids: Sequence[uuid.UUID],
        action: str = "anonymize",  # ruff: ignore[unused-function-argument]
    ) -> dict[str, int]:
        result = await db.execute(
            update(AgentShortlistEntry)
            .where(AgentShortlistEntry.id.in_(list(ids)))
            .values(draft_body=None, draft_subject=None, agent_note=None, explanation=None)
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    if "agent_shortlist_entries" not in retention_bindings():
        register_retention_binding(
            "agent_shortlist_entries",
            "agent_logs",
            subject_resolver=_subject,
            anchor_resolver=_anchor,
            expire_fn=_expire,
        )


for _fn in (
    _subscribe,
    _register_cap_counter,
    _register_notifications,
    _register_settings,
    _register_audit,
    _register_retention,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("sourcing_agent hooks: {} failed", _fn.__name__)
        raise
