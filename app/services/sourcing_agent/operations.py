# ruff: file-ignore[line-too-long]
"""Operations behind the Sourcing Agent tools (Epic 56, Part B). ``read_ctx`` is the data-scope context (Effective
Rights presented as the Accountable Owner); ``ctx`` is the tool's own context (actor ``agent``, or the approving person
when a Proposal executes). Nothing here writes ``matches`` / ``match_runs`` or any score (AD-8)."""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.sourcing_agent import AgentShortlist, AgentShortlistEntry
from app.services.sourcing_agent import contactability as contact
from app.services.sourcing_agent import outreach, service, shortlists, triggers


async def save_suggested(
    read_ctx: RequestContext,
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None,
    candidates: Sequence[Mapping[str, Any]],
    summary: str | None,
) -> dict[str, Any]:
    """``candidates``: ``[{candidate_id, sources: [{kind, ref?}], note?}]`` proposed by the agent (ids + tags only)."""
    settings = await service.settings_row(db, service._tenant_id(ctx, db))
    tags: dict[uuid.UUID, list[dict[str, Any]]] = {}
    notes: dict[uuid.UUID, str] = {}
    ids: list[uuid.UUID] = []
    for c in candidates:
        cid = uuid.UUID(str(c["candidate_id"]))
        ids.append(cid)
        tags.setdefault(cid, []).extend(dict(s) for s in c.get("sources") or [])
        if c.get("note"):
            notes[cid] = str(c["note"])[:300]
    cset = await service.build_candidate_set(
        read_ctx, db, demand_id, include_ids=ids, source_tags=tags, notes=notes,
        include_silver_medalists=settings.include_silver_medalists,
    )  # fmt: skip
    shortlist, entries, dropped = await shortlists.save_shortlist(
        ctx, db, demand_id, run_id=run_id, trigger_key=await triggers.run_trigger_key(db, run_id), rows=cset.rows,
        considered_count=cset.considered_count, excluded_counts=cset.excluded_counts, top_n=settings.top_n,
        preferred_channel=settings.preferred_channel, summary=summary,
    )  # fmt: skip
    return {
        "shortlist": shortlist,
        "entries": entries,
        "dropped": dropped,
        "excluded_counts": cset.excluded_counts,
        "dropped_not_found": cset.dropped_not_found,
        "top_n": settings.top_n,
        "draft_top": settings.draft_top,
    }


async def current_entry_for(
    read_ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    entry_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
) -> AgentShortlistEntry:
    """Return an entry of the Demand's **current** shortlist the scope context may read (else ``NotFound``), by
    entry id or by candidate id."""
    if entry_id is None:
        current = await shortlists.current(db, demand_id)
        if current is None or candidate_id is None:
            raise NotFound("Shortlist entry not found")
        entry_id = (
            await db.execute(
                select(AgentShortlistEntry.id).where(
                    AgentShortlistEntry.shortlist_id == current.id,
                    AgentShortlistEntry.candidate_id == candidate_id,
                )
            )
        ).scalar_one_or_none()
        if entry_id is None:
            raise NotFound("Shortlist entry not found")
    entry = await shortlists.readable_entry(read_ctx, db, entry_id, lock=True)
    shortlist = await db.get(AgentShortlist, entry.shortlist_id)
    if entry.demand_id != demand_id or shortlist is None or shortlist.status != "current":
        raise NotFound("Shortlist entry not found")
    if candidate_id is not None and entry.candidate_id != candidate_id:
        raise NotFound("Shortlist entry not found")
    return entry


async def prepare_draft(
    read_ctx: RequestContext,
    db: AsyncSession,
    entry: AgentShortlistEntry,
    *,
    channel: str,
    subject: str | None,
    body: str,
) -> tuple[str, str | None, tuple[str, ...]]:
    return await outreach.store_draft(read_ctx, db, entry, channel=channel, subject=subject, body=body)


async def attach_proposal(db: AsyncSession, entry: AgentShortlistEntry, proposal_id: uuid.UUID) -> None:
    entry.outreach_proposal_id = proposal_id
    entry.draft_status = "proposed"
    await db.flush()


async def cap_status(
    read_ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, candidate_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    from app.services.demands import matching_port

    await matching_port.get_demand(read_ctx, db, demand_id)
    facts, _ = await service.candidate_facts(read_ctx, db, candidate_ids)
    settings = await service.settings_row(db, service._tenant_id(read_ctx, db))
    return await contact.contactability(
        db, [c for c in candidate_ids if c in facts], preferred=settings.preferred_channel
    )


async def get_for_demand(read_ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any]:
    info = await service.demand_facts(read_ctx, db, demand_id)
    settings = await service.settings_row(db, service._tenant_id(read_ctx, db))
    row = await shortlists.current(db, demand_id)
    entries: list[AgentShortlistEntry] = []
    if row is not None:
        entries = list(
            (
                await db.execute(
                    select(AgentShortlistEntry)
                    .where(AgentShortlistEntry.shortlist_id == row.id)
                    .order_by(AgentShortlistEntry.rank)
                )
            ).scalars()
        )
    return {"info": info, "settings": settings, "shortlist": row, "entries": entries}


async def send_outreach(
    ctx: RequestContext,
    db: AsyncSession,
    entry_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    channel: str,
    subject: str | None,
    body: str,
    owner_user_id: uuid.UUID,
    approver_id: uuid.UUID,
    refs: dict[str, Any],
) -> tuple[uuid.UUID | None, str]:
    from app.services import entity_refs

    entry = await shortlists.readable_entry(ctx, db, entry_id, lock=True)
    if entry.candidate_id != candidate_id:
        raise NotFound("Shortlist entry not found")
    if entry.draft_status == "sent":
        from app.core.errors import ConflictError

        raise ConflictError("This outreach was already sent", code="already_sent")
    owner = await entity_refs.user(db, owner_user_id)
    return await outreach.send_approved(
        ctx, db, entry, channel=channel, subject=subject, body=body,
        owner_name=owner.name if owner is not None else "the recruiter", approver_id=approver_id, refs=refs,
    )  # fmt: skip


__all__ = [
    "attach_proposal",
    "cap_status",
    "current_entry_for",
    "get_for_demand",
    "prepare_draft",
    "save_suggested",
    "send_outreach",
]
