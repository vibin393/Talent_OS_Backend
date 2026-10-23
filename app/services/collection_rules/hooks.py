# ruff: file-ignore[line-too-long]
"""Registrations of the ``collection_rules`` domain (Epic 63), autodiscovered by ``core.hooks``.

* events (after commit; each enqueues a task):
  - P2 ``PromiseToPayBroken`` -> one Collections Agent escalation run (P2 already created the Task; no second Task);
  - foundation ``ProposalCreated`` / ``ProposalDecided`` of the ``collections`` agent -> ``message_proposed`` /
    ``proposal_*`` log rows; ``ProposalCreated`` of ``billing.prepare_run_draft`` -> notification
    ``invoice_run_prepared``;
  - ``CollectionsAgentHoldChanged`` -> notification ``collections_hold_changed`` (the collections owner, when someone
    else paused / took over);
  - P2 ``PaymentRecorded`` / ``DisputeStatusChanged``: nothing is mutated -- pending send Proposals go stale through
    the tools' ``ScopeRule.version`` (the Client's open balances + hold state).
* notification types, settings hub entry ``collection_rules``, workflow trigger ``collection_rules.message_sent``,
  invoice / client timeline sources, audit labels.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe

SEND_TOOLS = (
    "receivables.send_pre_due_reminder",
    "receivables.send_reminder",
    "receivables.send_dunning_message",
    "receivables.send_statement",
)


def _enqueue(task: Any, *args: Any) -> None:
    from app.tasks.runtime import enqueue

    enqueue(task, *args)


def _subscribe() -> None:
    from app.events.agents import ProposalCreated, ProposalDecided
    from app.events.collection_rules import CollectionsAgentHoldChanged
    from app.events.receivables import PromiseToPayBroken

    def on_ptp(e: PromiseToPayBroken) -> None:
        from app.tasks import collection_rules as tasks

        _enqueue(tasks.ptp_broken, str(e.tenant_id), str(e.client_id), str(e.promise_id))

    def on_created(e: ProposalCreated) -> None:
        from app.tasks import collection_rules as tasks
        from app.tasks import notifications as notification_tasks

        if e.agent_key == "collections" and e.tool_key in SEND_TOOLS:
            _enqueue(tasks.proposal_created, str(e.tenant_id), str(e.proposal_id))
        elif e.agent_key == "billing" and e.tool_key == "billing.prepare_run_draft":
            _enqueue(notification_tasks.fan_out, str(e.tenant_id), "invoice_run_prepared", {
                "event_id": f"invoice_run_prepared:{e.proposal_id}", "proposal_id": str(e.proposal_id),
                "assignee_user_id": str(e.assignee_user_id) if e.assignee_user_id else None,
                "route_role_key": e.route_role_key, "legal_entity_id": str(e.target_id) if e.target_id else None,
            })  # fmt: skip

    def on_decided(e: ProposalDecided) -> None:
        from app.tasks import collection_rules as tasks

        if e.agent_key == "collections" and e.tool_key in SEND_TOOLS:
            _enqueue(
                tasks.proposal_decided, str(e.tenant_id), str(e.proposal_id), e.outcome,
                str(e.decided_by) if e.decided_by else None,
            )  # fmt: skip

    def on_hold(e: CollectionsAgentHoldChanged) -> None:
        from app.tasks import notifications as notification_tasks

        _enqueue(notification_tasks.fan_out, str(e.tenant_id), "collections_hold_changed", {
            "event_id": f"collections_hold:{e.client_id}:{e.document_id}:{e.mode}:{e.active}:{uuid.uuid4().hex[:8]}",
            "client_id": str(e.client_id), "document_id": str(e.document_id) if e.document_id else None, "mode": e.mode,
            "active": e.active, "actor_id": str(e.actor_id),
        })  # fmt: skip

    for fn, name in ((on_ptp, "ptp_broken"), (on_created, "proposal_created"), (on_decided, "proposal_decided"), (on_hold, "hold")):
        fn.__name__ = f"collection_rules_on_{name}"
    subscribe(PromiseToPayBroken, on_ptp)
    subscribe(ProposalCreated, on_created)
    subscribe(ProposalDecided, on_decided)
    subscribe(CollectionsAgentHoldChanged, on_hold)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def run_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.services import directory_service

        out: set[uuid.UUID] = set()
        assignee = _uuid(payload.get("assignee_user_id"))
        if assignee is not None:
            out.add(assignee)
        role = payload.get("route_role_key")
        if role:
            role_id = await directory_service.role_id_by_key(db, str(role))
            if role_id is not None:
                out |= set(await directory_service.user_ids_with_role(db, role_id))
        return out

    async def run_render(db: AsyncSession, payload: Any) -> Any:
        from app.models.agent_runtime import AgentProposal

        p = await db.get(AgentProposal, _uuid(payload.get("proposal_id")))
        fields = dict((p.preview or {}).get("fields") or []) if p is not None else {}
        period = fields.get("Period", "The")
        title = f"{period} invoice run is ready to start: {fields.get('Drafts', '?')} drafts, {fields.get('Exceptions', '0')} exceptions"
        return ns.Content(
            title=title[:300], entity_type="agent_proposal", entity_id=p.id if p else None,
            link_path=f"/agents/proposals/{p.id}" if p else None,
        )  # fmt: skip

    async def hold_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.services.collection_rules.facts import collections_owner

        owner = await collections_owner(db, _uuid(payload.get("client_id")))
        actor = _uuid(payload.get("actor_id"))
        return {owner} if owner is not None and owner != actor else set()

    async def hold_render(db: AsyncSession, payload: Any) -> Any:
        from app.services import entity_refs

        cid = _uuid(payload.get("client_id"))
        name = await entity_refs.entity_label(db, "client", cid) if cid else None
        verb = ("taken over a thread" if payload.get("mode") == "takeover" else "paused") if payload.get("active") else "handed back"
        return ns.Content(
            title=f"Collections agent {verb} for {name or 'a client'}"[:300], entity_type="client", entity_id=cid,
            link_path=f"/receivables/collections?client={cid}" if cid else None,
        )  # fmt: skip

    defs = {
        "invoice_run_prepared": ns.NotificationType(
            "Invoice run ready to start", "AI agents", "both", False, run_recipients, run_render, internal_only=True
        ),
        "collections_hold_changed": ns.NotificationType(
            "Collections agent paused or taken over", "Receivables", "in_app", False, hold_recipients, hold_render,
            internal_only=True,
        ),
    }
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "finance", "collection_rules", "Collection rules",
        "Reminder schedule, tone, contacts and pre-due automatic reminders per client.", "/api/v1/collections/rules",
        "collections:write", owner_epic="Epic 63",
    )  # fmt: skip


def _register_workflow_trigger() -> None:
    try:
        from app.services.workflow.registry import TRIGGERS, FieldDef, Trigger, register_trigger
    except ImportError:  # pragma: no cover - workflow absent
        return
    key = "collection_rules.message_sent"
    if key in TRIGGERS:
        return
    register_trigger(
        key,
        Trigger(
            key, "Collections Agent message sent", "app.events.collection_rules", "CollectionMessageSent", "client",
            "client_id", {"tone": ("tone", FieldDef("enum", "Tone", ("courteous", "friendly", "firm", "escalation")))},
        ),
    )  # fmt: skip


def _register_audit_and_timeline() -> None:
    from app.core import audit
    from app.services.collection_rules import log, timeline

    for event, label in log.LABELS.items():
        audit.register_audit_action(f"collections_agent.{event}", f"Collections agent: {label}")
    timeline.register()


for _fn in (_subscribe, _register_notifications, _register_settings, _register_workflow_trigger, _register_audit_and_timeline):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("collection_rules hooks: {} failed", _fn.__name__)
        raise
