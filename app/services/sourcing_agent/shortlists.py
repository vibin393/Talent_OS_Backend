# ruff: noqa: E501
"""Agent shortlists (Epic 56 stories 56.2 / 56.5, FR-286, FR-323).

* ``save_shortlist`` -- supersedes the current version and inserts version + 1 in deterministic order
  (``ranking.rank``), applies the Demand's dismissals ("dismissed stays dismissed"), marks ``new`` (not in the
  previous version) and ``dropped_back`` (listed in an older version, missing from the previous one, back now) and
  emits ``AgentShortlistSaved``. Two concurrent saves: the partial unique index ``uq_agent_shortlists_current`` makes
  the loser retry once as the next version.
* ``decide_entry`` -- a person's decision (``add_to_pipeline`` / ``dismiss`` / ``restore``). Adding calls the P1
  ``pipeline_service.add_candidate(source="ai_match", source_ref="agent_shortlist:<entry>")``; every decision emits
  ``AgentShortlistEntryDecided`` (the FR-323 selection event) and is audited.
* ``mark_added_via_proposal`` -- the same for an approved ``pipeline.add_candidate`` Proposal (``via_proposal``).
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.sourcing_agent import AgentShortlistEntryDecided, AgentShortlistSaved
from app.models.sourcing_agent import AgentShortlist, AgentShortlistDismissal, AgentShortlistEntry
from app.schemas.sourcing_agent import ChannelState, EntryOut, ShortlistOut, ShortlistVersionOut
from app.services.sourcing_agent import contactability as contact
from app.services.sourcing_agent import ranking


def _now() -> datetime:
    return datetime.now(UTC)


def _actor_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if principal is not None else None


# ================================================================ reads
async def _demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.services.demands import matching_port

    return await matching_port.get_demand(ctx, db, demand_id)


async def _entries(db: AsyncSession, shortlist_id: uuid.UUID) -> list[AgentShortlistEntry]:
    return list(
        (
            await db.execute(
                select(AgentShortlistEntry)
                .where(AgentShortlistEntry.shortlist_id == shortlist_id)
                .order_by(AgentShortlistEntry.rank)
            )
        ).scalars()
    )


def entry_out(e: AgentShortlistEntry, names: Mapping[uuid.UUID, Mapping[str, Any]] | None = None) -> EntryOut:
    info = (names or {}).get(e.candidate_id) or {}
    return EntryOut(
        id=e.id, shortlist_id=e.shortlist_id, demand_id=e.demand_id, candidate_id=e.candidate_id,
        candidate_name=info.get("full_name"), candidate_human_id=info.get("human_id"),
        current_title=info.get("current_title"), rank=e.rank,
        overall=float(e.overall) if e.overall is not None else None, match_id=e.match_id,
        sources=list(e.sources or []), explanation=e.explanation, agent_note=e.agent_note, flags=list(e.flags or []),
        contactability={k: ChannelState(**v) for k, v in (e.contactability or {}).items() if isinstance(v, dict)},
        best_channel=e.best_channel, last_activity_at=e.last_activity_at,  # type: ignore[arg-type]
        notice_period_days=e.notice_period_days, availability_date=e.availability_date, status=e.status,  # type: ignore[arg-type]
        decided_by=e.decided_by, decided_at=e.decided_at, dismiss_reason=e.dismiss_reason,
        application_id=e.application_id, draft_channel=e.draft_channel, draft_subject=e.draft_subject,  # type: ignore[arg-type]
        draft_body=e.draft_body, draft_status=e.draft_status, outreach_proposal_id=e.outreach_proposal_id,  # type: ignore[arg-type]
        outreach_sent_at=e.outreach_sent_at,
    )  # fmt: skip


async def _names(db: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    from app.models.candidates import Candidate

    if not ids:
        return {}
    rows = await db.execute(
        select(Candidate.id, Candidate.full_name, Candidate.human_id, Candidate.current_title).where(
            Candidate.id.in_(list(ids))
        )
    )
    return {
        r.id: {"full_name": r.full_name, "human_id": r.human_id, "current_title": r.current_title}
        for r in rows
    }


async def shortlist_out(db: AsyncSession, s: AgentShortlist) -> ShortlistOut:
    entries = await _entries(db, s.id)
    names = await _names(db, [e.candidate_id for e in entries])
    return ShortlistOut(
        id=s.id, demand_id=s.demand_id, run_id=s.run_id, version=s.version, status=s.status,  # type: ignore[arg-type]
        trigger_key=s.trigger_key, considered_count=s.considered_count,  # type: ignore[arg-type]
        excluded_counts={k: int(v) for k, v in (s.excluded_counts or {}).items()},
        suppressed_counts={k: int(v) for k, v in (s.suppressed_counts or {}).items()}, top_n=s.top_n,
        summary=s.summary, created_at=s.created_at, entries=[entry_out(e, names) for e in entries],
    )  # fmt: skip


async def current(db: AsyncSession, demand_id: uuid.UUID) -> AgentShortlist | None:
    return (
        await db.execute(
            select(AgentShortlist).where(
                AgentShortlist.demand_id == demand_id, AgentShortlist.status == "current"
            )
        )
    ).scalar_one_or_none()


async def get_current(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> ShortlistOut | None:
    await _demand(ctx, db, demand_id)
    row = await current(db, demand_id)
    return await shortlist_out(db, row) if row is not None else None


async def get_version(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, version: int
) -> ShortlistOut:
    await _demand(ctx, db, demand_id)
    row = (
        await db.execute(
            select(AgentShortlist).where(
                AgentShortlist.demand_id == demand_id, AgentShortlist.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Shortlist version not found")
    return await shortlist_out(db, row)


async def list_versions(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
) -> list[ShortlistVersionOut]:
    await _demand(ctx, db, demand_id)
    counts = (
        select(AgentShortlistEntry.shortlist_id, func.count().label("n"))
        .group_by(AgentShortlistEntry.shortlist_id)
        .subquery()
    )
    rows = await db.execute(
        select(AgentShortlist, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.shortlist_id == AgentShortlist.id)
        .where(AgentShortlist.demand_id == demand_id)
        .order_by(AgentShortlist.version.desc())
    )
    return [
        ShortlistVersionOut(
            id=s.id,
            version=s.version,
            status=s.status,
            trigger_key=s.trigger_key,
            created_at=s.created_at,  # type: ignore[arg-type]
            entry_count=int(n),
        )
        for s, n in rows.all()
    ]


async def dismissed_ids(db: AsyncSession, demand_id: uuid.UUID) -> dict[uuid.UUID, AgentShortlistDismissal]:
    rows = (
        await db.execute(
            select(AgentShortlistDismissal).where(AgentShortlistDismissal.demand_id == demand_id)
        )
    ).scalars()
    return {d.candidate_id: d for d in rows}


# ================================================================ save
async def _history(db: AsyncSession, demand_id: uuid.UUID) -> tuple[set[uuid.UUID] | None, set[uuid.UUID]]:
    """``(previous version ids or None, ids of every older version)``."""
    versions = list(
        (
            await db.execute(
                select(AgentShortlist.id, AgentShortlist.version)
                .where(AgentShortlist.demand_id == demand_id)
                .order_by(AgentShortlist.version.desc())
            )
        ).all()
    )
    if not versions:
        return None, set()
    latest_id = versions[0][0]
    rows = await db.execute(
        select(AgentShortlistEntry.shortlist_id, AgentShortlistEntry.candidate_id).where(
            AgentShortlistEntry.shortlist_id.in_([v[0] for v in versions])
        )
    )
    previous: set[uuid.UUID] = set()
    older: set[uuid.UUID] = set()
    for sid, cid in rows.all():
        (previous if sid == latest_id else older).add(cid)
    return previous, older


async def save_shortlist(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None,
    trigger_key: str,
    rows: Sequence[Mapping[str, Any]],
    considered_count: int,
    excluded_counts: Mapping[str, int],
    top_n: int,
    preferred_channel: str | None,
    summary: str | None,
) -> tuple[AgentShortlist, list[AgentShortlistEntry], int]:
    """Save version + 1; return ``(shortlist, entries, dropped_count)``."""
    ranked = ranking.rank(rows)
    dismissals = await dismissed_ids(db, demand_id)
    chosen = ranking.select_top(ranked, top_n, lambda r: r["candidate_id"] in dismissals)
    contacts = await contact.contactability(
        db,
        [r["candidate_id"] for r in chosen if r["candidate_id"] not in dismissals],
        preferred=preferred_channel,
    )
    for attempt in (1, 2):
        try:
            async with db.begin_nested():
                return await _insert(
                    ctx, db, demand_id, run_id=run_id, trigger_key=trigger_key, chosen=chosen, contacts=contacts,
                    dismissals=dismissals, considered_count=considered_count, excluded_counts=excluded_counts,
                    top_n=top_n, summary=summary,
                )  # fmt: skip
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


async def _insert(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    run_id: uuid.UUID | None,
    trigger_key: str,
    chosen: Sequence[Mapping[str, Any]],
    contacts: Mapping[uuid.UUID, Any],
    dismissals: Mapping[uuid.UUID, AgentShortlistDismissal],
    considered_count: int,
    excluded_counts: Mapping[str, int],
    top_n: int,
    summary: str | None,
) -> tuple[AgentShortlist, list[AgentShortlistEntry], int]:
    from app.services import entity_refs

    previous, older = await _history(db, demand_id)
    version = int(
        (
            await db.execute(select(func.coalesce(func.max(AgentShortlist.version), 0)).where(AgentShortlist.demand_id == demand_id))
        ).scalar_one()
    ) + 1  # fmt: skip
    await db.execute(
        update(AgentShortlist)
        .where(AgentShortlist.demand_id == demand_id, AgentShortlist.status == "current")
        .values(status="superseded")
    )
    tenant_id = ctx.tenant_id or entity_refs.session_tenant(db)
    shortlist = AgentShortlist(
        id=uuid.uuid4(), tenant_id=tenant_id, demand_id=demand_id, run_id=run_id, version=version, status="current",
        trigger_key=trigger_key, considered_count=considered_count, excluded_counts=dict(excluded_counts),
        suppressed_counts=contact.suppressed_counts(contacts.values()), top_n=top_n,
        summary=(summary or None) and summary[:2000],
    )  # fmt: skip
    db.add(shortlist)
    await db.flush()
    entries: list[AgentShortlistEntry] = []
    new_count = 0
    chosen_ids = {r["candidate_id"] for r in chosen}
    for rank_no, row in enumerate(chosen, start=1):
        cid = row["candidate_id"]
        flags = list(row.get("flags") or [])
        if previous is not None and cid not in previous:
            if cid in older:
                flags.append("dropped_back")
            else:
                flags.append("new")
                new_count += 1
        dismissal = dismissals.get(cid)
        c = contacts.get(cid)
        if c is not None:
            contact_json = contact.as_json(c)
            best = c.best_channel
        else:
            contact_json = {ch: {"ok": False, "reason": "dismissed"} for ch in contact.CHANNELS}
            best = None
        entry = AgentShortlistEntry(
            id=uuid.uuid4(), tenant_id=tenant_id, shortlist_id=shortlist.id, demand_id=demand_id, candidate_id=cid,
            rank=rank_no, overall=row.get("overall"), match_id=row.get("match_id"), sources=list(row.get("sources") or []),
            explanation=row.get("explanation"), agent_note=(row.get("note") or None) and str(row["note"])[:300],
            flags=sorted(set(flags)), contactability=contact_json, best_channel=best,
            last_activity_at=row.get("last_activity_at"), notice_period_days=row.get("notice_period_days"),
            availability_date=row.get("availability_date"),
            status="dismissed" if dismissal is not None else "suggested",
            dismiss_reason=dismissal.reason if dismissal is not None else None,
            decided_by=dismissal.dismissed_by if dismissal is not None else None,
            decided_at=dismissal.dismissed_at if dismissal is not None else None, draft_status="none",
        )  # fmt: skip
        db.add(entry)
        entries.append(entry)
    await db.flush()
    dropped = len(previous - chosen_ids) if previous is not None else 0
    emit(db, AgentShortlistSaved(
        tenant_id=tenant_id, shortlist_id=shortlist.id, demand_id=demand_id, run_id=run_id,
        entry_count=len(entries), new_count=new_count, dropped_count=dropped,
    ))  # fmt: skip
    audit.record(
        "sourcing.shortlist_saved", "agent_shortlist", shortlist.id, session=db, demand_id=str(demand_id),
        version=version, entries=len(entries),
    )  # fmt: skip
    return shortlist, entries, dropped


# ================================================================ decisions
async def _entry(db: AsyncSession, entry_id: uuid.UUID, *, lock: bool = False) -> AgentShortlistEntry:
    stmt = select(AgentShortlistEntry).where(AgentShortlistEntry.id == entry_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Shortlist entry not found")
    return row


async def readable_entry(
    ctx: RequestContext, db: AsyncSession, entry_id: uuid.UUID, *, lock: bool = False
) -> AgentShortlistEntry:
    """Return the entry if ``ctx`` may read its Demand and the Candidate (404 otherwise)."""
    from app.services import entity_refs

    entry = await _entry(db, entry_id, lock=lock)
    await _demand(ctx, db, entry.demand_id)
    if not await entity_refs.can_read(ctx, db, "candidate", entry.candidate_id):
        raise NotFound("Shortlist entry not found")
    return entry


def _emit_decided(
    db: AsyncSession, ctx: RequestContext, entry: AgentShortlistEntry, decision: str, *, via_proposal: bool
) -> None:
    emit(db, AgentShortlistEntryDecided(
        tenant_id=entry.tenant_id, entry_id=entry.id, shortlist_id=entry.shortlist_id, demand_id=entry.demand_id,
        candidate_id=entry.candidate_id, decision=decision, actor_kind=actor_from_ctx(ctx).kind, via_proposal=via_proposal,
    ))  # fmt: skip


async def _add(ctx: RequestContext, db: AsyncSession, entry: AgentShortlistEntry) -> uuid.UUID:
    from app.services import pipeline_service

    return await pipeline_service.add_candidate(
        ctx,
        db,
        entry.demand_id,
        entry.candidate_id,
        source="ai_match",
        source_ref=f"agent_shortlist:{entry.id}",
    )


async def decide_entry(
    ctx: RequestContext, db: AsyncSession, entry_id: uuid.UUID, decision: str, reason: str | None = None
) -> EntryOut:
    entry = await readable_entry(ctx, db, entry_id, lock=True)
    shortlist = await db.get(AgentShortlist, entry.shortlist_id)
    if shortlist is None or shortlist.status != "current":
        raise ConflictError(
            "This shortlist version was replaced; open the current one", code="shortlist_superseded"
        )
    now = _now()
    actor = _actor_id(ctx)
    if decision == "add_to_pipeline":
        if entry.status == "added_to_pipeline":
            raise ConflictError("Already in the pipeline", code="already_added")
        entry.application_id = await _add(ctx, db, entry)
        entry.status, entry.decided_by, entry.decided_at, entry.dismiss_reason = (
            "added_to_pipeline",
            actor,
            now,
            None,
        )
        out_decision = "added_to_pipeline"
    elif decision == "dismiss":
        if not reason:
            raise ValidationFailed(
                "Pick a reason", code="reason_required", details={"fields": {"reason": "Required"}}
            )
        if entry.status == "added_to_pipeline":
            raise ConflictError("Already in the pipeline", code="already_added")
        existing = (
            await db.execute(
                select(AgentShortlistDismissal).where(
                    AgentShortlistDismissal.demand_id == entry.demand_id,
                    AgentShortlistDismissal.candidate_id == entry.candidate_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(AgentShortlistDismissal(
                id=uuid.uuid4(), tenant_id=entry.tenant_id, demand_id=entry.demand_id, candidate_id=entry.candidate_id,
                reason=reason, dismissed_by=actor, dismissed_at=now,
            ))  # fmt: skip
        else:
            existing.reason, existing.dismissed_by, existing.dismissed_at = reason, actor, now  # type: ignore[assignment]
        entry.status, entry.dismiss_reason, entry.decided_by, entry.decided_at = (
            "dismissed",
            reason,
            actor,
            now,
        )
        out_decision = "dismissed"
    elif decision == "restore":
        if entry.status != "dismissed":
            raise ConflictError("Only dismissed entries can be restored", code="not_dismissed")
        await db.execute(
            delete(AgentShortlistDismissal).where(
                AgentShortlistDismissal.demand_id == entry.demand_id,
                AgentShortlistDismissal.candidate_id == entry.candidate_id,
            )
        )
        entry.status, entry.dismiss_reason, entry.decided_by, entry.decided_at = "suggested", None, actor, now
        out_decision = "restored"
    else:  # pragma: no cover - schema-validated
        raise ValidationFailed("Unknown decision", code="invalid_decision")
    await db.flush()
    _emit_decided(db, ctx, entry, out_decision, via_proposal=False)
    audit.record(
        f"sourcing.entry_{out_decision}", "agent_shortlist_entry", entry.id, session=db,
        candidate_id=str(entry.candidate_id), demand_id=str(entry.demand_id), reason=reason,
    )  # fmt: skip
    names = await _names(db, [entry.candidate_id])
    return entry_out(entry, names)


async def mark_added_via_proposal(
    ctx: RequestContext, db: AsyncSession, entry: AgentShortlistEntry, application_id: uuid.UUID
) -> None:
    if entry.status == "added_to_pipeline":
        return
    entry.application_id = application_id
    entry.status, entry.decided_by, entry.decided_at, entry.dismiss_reason = (
        "added_to_pipeline",
        _actor_id(ctx),
        _now(),
        None,
    )
    await db.flush()
    _emit_decided(db, ctx, entry, "added_to_pipeline", via_proposal=True)
    audit.record(
        "sourcing.entry_added_to_pipeline", "agent_shortlist_entry", entry.id, session=db,
        candidate_id=str(entry.candidate_id), demand_id=str(entry.demand_id), via_proposal=True,
    )  # fmt: skip


async def add_via_proposal(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    candidate_id: uuid.UUID,
    entry_id: uuid.UUID | None,
) -> uuid.UUID:
    """Execute an approved ``pipeline.add_candidate`` Proposal (``ctx`` = the approving person)."""
    from app.services import pipeline_service

    entry = None
    if entry_id is not None:
        entry = await _entry(db, entry_id, lock=True)
        if entry.demand_id != demand_id or entry.candidate_id != candidate_id:
            raise NotFound("Shortlist entry not found")
    ref = f"agent_shortlist:{entry_id}" if entry_id is not None else "agent_shortlist"
    application_id = await pipeline_service.add_candidate(
        ctx, db, demand_id, candidate_id, source="ai_match", source_ref=ref
    )
    if entry is not None:
        await mark_added_via_proposal(ctx, db, entry, application_id)
    return application_id


# ================================================================ drafts / outreach state
async def entry_version(db: AsyncSession, entry_id: uuid.UUID) -> str | None:
    value = (
        await db.execute(select(AgentShortlistEntry.updated_at).where(AgentShortlistEntry.id == entry_id))
    ).scalar_one_or_none()
    return value.isoformat() if value is not None else None


async def on_proposal_decided(db: AsyncSession, proposal_id: uuid.UUID, status: str) -> int:
    """``outreach.send`` Proposal decided: reflect it on the entry (``executed`` was marked by the send itself)."""
    from app.models.agent_runtime import AgentProposal

    entry = (
        await db.execute(
            select(AgentShortlistEntry)
            .where(AgentShortlistEntry.outreach_proposal_id == proposal_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if entry is None or entry.draft_status == "sent":
        return 0
    if status == "execution_failed":
        proposal = await db.get(AgentProposal, proposal_id)
        code = (proposal.error_code or "") if proposal is not None else ""
        entry.draft_status = "suppressed" if code.startswith("suppressed:") else "failed"
    elif status in ("rejected", "expired", "stale", "discarded"):
        entry.draft_status = "none"
        entry.outreach_proposal_id = None
    else:
        return 0
    await db.flush()
    return 1


__all__ = [
    "add_via_proposal",
    "current",
    "decide_entry",
    "dismissed_ids",
    "entry_out",
    "entry_version",
    "get_current",
    "get_version",
    "list_versions",
    "mark_added_via_proposal",
    "on_proposal_decided",
    "readable_entry",
    "save_shortlist",
    "shortlist_out",
]
