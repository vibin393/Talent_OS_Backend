# ruff: file-ignore[line-too-long]
"""The Collections Agent log (Epic 63 A4, FR-315): append-only rows behind the Invoice / Client collection timeline.

``write`` also records an audit event on the log row (``collections_agent.<event>``) so the P2 record timelines pick
the entry up through the ``invoice`` / ``client`` timeline sources (``timeline.py``). ``summary`` never carries amounts
or Contact PII. ``on_proposal_decided`` turns ``ProposalDecided`` of the ``collections`` agent into the matching
``proposal_*`` rows.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.collection_rules import CollectionAgentLog
from app.schemas.collection_rules import LogOut

LABELS: dict[str, str] = {
    "reminder_sent": "Sent by Collections Agent",
    "message_sent": "Sent by Collections Agent",
    "message_proposed": "Drafted by Collections Agent — awaiting approval",
    "proposal_approved": "Approved",
    "proposal_edited_approved": "Edited and approved",
    "proposal_rejected": "Rejected",
    "proposal_expired": "Proposal expired",
    "proposal_stale": "Proposal out of date",
    "held": "Paused",
    "takeover": "Taken over",
    "released": "Handed back to agent",
}
OUTCOME_EVENTS = {
    "approved": "proposal_approved",
    "edited_approved": "proposal_edited_approved",
    "rejected": "proposal_rejected",
    "expired": "proposal_expired",
    "stale": "proposal_stale",
}
SEND_TOOLS = (
    "receivables.send_pre_due_reminder",
    "receivables.send_reminder",
    "receivables.send_dunning_message",
    "receivables.send_statement",
)


async def write(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
    event: str,
    actor: dict[str, Any],
    summary: str | None = None,
    rule_step_id: uuid.UUID | None = None,
    tone: str | None = None,
    email_message_id: uuid.UUID | None = None,
    collection_activity_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
    proposal_id: uuid.UUID | None = None,
) -> CollectionAgentLog:
    from app.services import entity_refs

    row = CollectionAgentLog(
        id=uuid.uuid4(), tenant_id=entity_refs.session_tenant(db), client_id=client_id, document_ids=list(document_ids),
        event=event, rule_step_id=rule_step_id, tone=tone, email_message_id=email_message_id,
        collection_activity_id=collection_activity_id, agent_run_id=agent_run_id, proposal_id=proposal_id,
        actor=actor, summary=(summary or "")[:300] or None,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    audit.record(
        f"collections_agent.{event}", "collection_agent_log", row.id, session=db, client_id=str(client_id),
        documents=[str(d) for d in document_ids], proposal_id=str(proposal_id) if proposal_id else None,
    )  # fmt: skip
    return row


async def on_proposal_decided(
    db: AsyncSession, proposal_id: uuid.UUID, outcome: str, decided_by: uuid.UUID | None
) -> int:
    """``ProposalDecided`` (agent ``collections``, a send tool) -> one ``proposal_*`` row on the Client / documents."""
    from app.models.agent_runtime import AgentProposal

    event = OUTCOME_EVENTS.get(outcome)
    p = await db.get(AgentProposal, proposal_id)
    if event is None or p is None or p.agent_key != "collections" or p.tool_key not in SEND_TOOLS:
        return 0
    args = dict(p.args or {})
    try:
        client_id = uuid.UUID(str(args.get("client_id")))
    except ValueError:
        return 0
    docs = [uuid.UUID(str(d)) for d in args.get("document_ids") or []]
    dup = (
        await db.execute(
            select(CollectionAgentLog.id).where(CollectionAgentLog.proposal_id == proposal_id, CollectionAgentLog.event == event)
        )
    ).first()
    if dup is not None:
        return 0
    await write(
        db, client_id=client_id, document_ids=docs, event=event,
        actor={"kind": "user", "id": str(decided_by)} if decided_by else {"kind": "system"},
        summary=f"{LABELS[event]} ({p.human_id})", agent_run_id=p.run_id, proposal_id=p.id,
        tone=args.get("tone") if isinstance(args.get("tone"), str) else None,
    )  # fmt: skip
    return 1


async def on_proposal_created(db: AsyncSession, proposal_id: uuid.UUID) -> int:
    """``ProposalCreated`` (agent ``collections``, a send tool) -> one ``message_proposed`` row (the step then counts
    as done for its documents, so the next daily scan does not draft it again)."""
    from app.models.agent_runtime import AgentProposal
    from app.models.collection_rules import CollectionRuleStep

    p = await db.get(AgentProposal, proposal_id)
    if p is None or p.agent_key != "collections" or p.tool_key not in SEND_TOOLS:
        return 0
    dup = (
        await db.execute(
            select(CollectionAgentLog.id).where(
                CollectionAgentLog.proposal_id == proposal_id, CollectionAgentLog.event == "message_proposed"
            )
        )
    ).first()
    if dup is not None:
        return 0
    args = dict(p.args or {})
    try:
        client_id = uuid.UUID(str(args.get("client_id")))
        step_id = uuid.UUID(str(args["step_id"])) if args.get("step_id") else None
    except ValueError:
        return 0
    tone = args.get("tone") if isinstance(args.get("tone"), str) else None
    if step_id is not None and tone is None:
        step = await db.get(CollectionRuleStep, step_id)
        tone = step.tone if step is not None else None
    await write(
        db, client_id=client_id, document_ids=[uuid.UUID(str(d)) for d in args.get("document_ids") or []],
        event="message_proposed", actor={"kind": "agent", "run_id": str(p.run_id) if p.run_id else None},
        summary=f"Drafted by Collections Agent - awaiting approval ({p.human_id})", rule_step_id=step_id, tone=tone,
        agent_run_id=p.run_id, proposal_id=p.id,
    )  # fmt: skip
    return 1


async def list_log(
    db: AsyncSession, *, client_id: uuid.UUID | None = None, document_id: uuid.UUID | None = None, limit: int = 200
) -> list[LogOut]:
    from app.models.agent_runtime import AgentProposal
    from app.services import entity_refs

    stmt = select(CollectionAgentLog)
    if client_id is not None:
        stmt = stmt.where(CollectionAgentLog.client_id == client_id)
    if document_id is not None:
        stmt = stmt.where(CollectionAgentLog.document_ids.contains([document_id]))
    rows = list((await db.execute(stmt.order_by(CollectionAgentLog.at.desc()).limit(limit))).scalars())
    prop_ids = [r.proposal_id for r in rows if r.proposal_id]
    human = (
        dict((await db.execute(select(AgentProposal.id, AgentProposal.human_id).where(AgentProposal.id.in_(prop_ids)))).tuples().all())
        if prop_ids
        else {}
    )
    user_ids = []
    for r in rows:
        try:
            if (r.actor or {}).get("kind") == "user" and r.actor.get("id"):
                user_ids.append(uuid.UUID(str(r.actor["id"])))
        except ValueError:
            continue
    users = await entity_refs.users(db, user_ids) if user_ids else {}
    out = []
    for r in rows:
        actor_name = None
        try:
            uid = uuid.UUID(str((r.actor or {}).get("id"))) if (r.actor or {}).get("id") else None
        except ValueError:
            uid = None
        if uid is not None and uid in users:
            actor_name = users[uid].name
        label = LABELS.get(r.event, r.event)
        hid = human.get(r.proposal_id) if r.proposal_id else None
        if r.event == "message_proposed" and hid:
            label = f"{label} ({hid})"
        elif r.event.startswith("proposal_") and actor_name and r.event != "proposal_expired":
            label = f"{label} by {actor_name}"
        elif r.event in ("held", "takeover", "released") and actor_name:
            label = f"{LABELS[r.event]} by {actor_name}"
        out.append(
            LogOut(
                id=r.id, client_id=r.client_id, document_ids=list(r.document_ids or []), event=r.event, label=label,
                rule_step_id=r.rule_step_id, tone=r.tone, email_message_id=r.email_message_id,
                collection_activity_id=r.collection_activity_id, agent_run_id=r.agent_run_id, proposal_id=r.proposal_id,
                proposal_human_id=hid, actor=dict(r.actor or {}), actor_name=actor_name, summary=r.summary, at=r.at,
            )
        )  # fmt: skip
    return out


async def done_steps(db: AsyncSession, client_id: uuid.UUID) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """(document, step) pairs already sent or pending approval -- a step runs once per document."""
    rows = (
        await db.execute(
            select(CollectionAgentLog.document_ids, CollectionAgentLog.rule_step_id).where(
                CollectionAgentLog.client_id == client_id,
                CollectionAgentLog.rule_step_id.is_not(None),
                CollectionAgentLog.event.in_(("reminder_sent", "message_sent", "message_proposed")),
            )
        )
    ).all()
    return {(d, step) for docs, step in rows for d in docs or []}


__all__ = ["LABELS", "OUTCOME_EVENTS", "SEND_TOOLS", "done_steps", "list_log", "on_proposal_created", "on_proposal_decided", "write"]
