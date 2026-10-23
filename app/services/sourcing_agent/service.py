# ruff: file-ignore[line-too-long]
"""Sourcing domain service (Epic 56, FR-285): settings, Demand facts and the candidate set.

``build_candidate_set`` gathers known talent from the Tenant's own data only (never job boards / social / third
parties, PRD §9): the deterministic P1 match list (``list_matches``, 500 rows), pinned Pools and the rediscovery
``pools`` section, silver medalists (P3 ``silver_medalists`` template Pools, when enabled) and the ids the agent found
with its structured searches. Non-match ids are scored read-only with ``scores_for_candidates`` (AD-8: nothing is
recomputed or written); unscored ids rank after scored ones. Exclusions are counted:

* ``in_pipeline`` -- already an Application of this Demand;
* ``do_not_contact`` -- marketing consent withdrawn and every channel opted out (Known gap 1);
* ``archived`` -- archived, erased or merged Candidates;
* ``placed`` -- an active Placement, unless the Candidate is in a Pool created from the ``contractors_ending``
  template;
* ``no_ai_consent`` -- under ``consent_required``, Candidates without AI-processing consent not found by keyword
  search (they appear only from search, flagged ``no_ai_consent``, without an explanation).

Reads run under ``ctx`` -- for agent tools the scope context (Effective Rights presented as the Accountable Owner).
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal, scope_condition
from app.models.candidates import Candidate
from app.models.placements import Placement
from app.models.sourcing_agent import SourcingAgentSettings
from app.models.talent_pools import TalentPool, TalentPoolMember
from app.schemas.sourcing_agent import CandidateSet, SourcingSettingsIn, SourcingSettingsOut

MATCH_PAGE = 500
POOL_MEMBER_LIMIT = 2000
ACTIVE_PLACEMENT_STATUSES = ("pre_joining", "joining_confirmed", "active")
CONSENTED_STATES = ("granted", "not_required")
_tenants = table("tenants", column("id"), column("slug"))


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal):
        raise NotFound("Resource not found")
    return principal


# ================================================================ settings
async def settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> SourcingAgentSettings:
    row = await db.get(SourcingAgentSettings, tenant_id)
    if row is None:
        row = SourcingAgentSettings(
            tenant_id=tenant_id, top_n=25, draft_top=10, preferred_channel="email", include_silver_medalists=True,
            max_searches=3, propose_pipeline_adds=False, propose_sequence_id=None,
        )  # fmt: skip
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def settings_out(row: SourcingAgentSettings) -> SourcingSettingsOut:
    return SourcingSettingsOut(
        top_n=row.top_n, draft_top=row.draft_top, preferred_channel=row.preferred_channel,
        include_silver_medalists=row.include_silver_medalists, max_searches=row.max_searches,
        propose_pipeline_adds=row.propose_pipeline_adds, propose_sequence_id=row.propose_sequence_id,
        updated_at=row.updated_at,
    )  # fmt: skip


def _tenant_id(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    from app.services import entity_refs

    return ctx.tenant_id or entity_refs.session_tenant(db)


async def get_settings_(ctx: RequestContext, db: AsyncSession) -> SourcingSettingsOut:
    return settings_out(await settings_row(db, _tenant_id(ctx, db)))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: SourcingSettingsIn
) -> SourcingSettingsOut:
    row = await settings_row(db, _tenant_id(ctx, db))
    changes = data.model_dump(exclude_unset=True, exclude={"clear_sequence"})
    if data.clear_sequence:
        changes["propose_sequence_id"] = None
    if changes.get("propose_sequence_id") is not None:
        from app.models.campaigns import Sequence

        if await db.get(Sequence, changes["propose_sequence_id"]) is None:
            raise NotFound("Sequence not found")
    for key, value in changes.items():
        if value is not None or key == "propose_sequence_id":
            setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    audit.record(
        "sourcing.settings_changed",
        "sourcing_agent_settings",
        row.tenant_id,
        session=db,
        fields=sorted(changes),
    )
    return settings_out(row)


# ================================================================ demand facts
async def demand_facts(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any]:
    """Facts the agent may use to build searches and drafts (no protected attributes, no client secrets)."""
    from app.models.demands import DemandPosting, DemandSkill
    from app.services.demands import matching_port

    demand = await matching_port.get_demand(ctx, db, demand_id)
    skills = list(
        (
            await db.execute(
                select(DemandSkill)
                .where(DemandSkill.demand_id == demand_id)
                .order_by(DemandSkill.kind, DemandSkill.position)
            )
        ).scalars()
    )
    posting = (
        await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand_id))
    ).scalar_one_or_none()
    return {
        "demand": demand,
        "title": demand.title,
        "status": demand.status,
        "location": demand.location,
        "country": demand.country,
        "work_mode": demand.work_mode,
        "experience_min": demand.min_experience_years,
        "experience_max": demand.max_experience_years,
        "must_have": [
            {"skill_id": s.skill_id, "label": s.skill_label} for s in skills if s.kind == "mandatory"
        ],
        "nice_to_have": [
            {"skill_id": s.skill_id, "label": s.skill_label} for s in skills if s.kind == "preferred"
        ],
        "posting": posting,
    }


async def posting_url(db: AsyncSession, posting: Any) -> str | None:
    from app.core.settings import get_settings

    if posting is None or not posting.is_live:
        return None
    slug = (
        await db.execute(select(_tenants.c.slug).where(_tenants.c.id == posting.tenant_id))
    ).scalar_one_or_none()
    if not slug:
        return None
    return f"{get_settings().frontend_url.rstrip('/')}/careers/{slug}/jobs/{posting.id}"


# ================================================================ candidate facts
async def candidate_facts(
    ctx: RequestContext, db: AsyncSession, candidate_ids: Iterable[uuid.UUID]
) -> tuple[dict[uuid.UUID, dict[str, Any]], set[uuid.UUID]]:
    """``(visible_facts, invisible_or_unknown_ids)``: facts of the ids the context may read (any status)."""
    ids = list(dict.fromkeys(candidate_ids))
    if not ids:
        return {}, set()
    cond = (
        scope_condition(_principal(ctx), "candidate:read", Candidate.owner_id)
        if ctx.principal is not None
        else None
    )
    facts: dict[uuid.UUID, dict[str, Any]] = {}
    for start in range(0, len(ids), 5000):
        chunk = ids[start : start + 5000]
        stmt = select(
            Candidate.id, Candidate.full_name, Candidate.human_id, Candidate.current_title, Candidate.updated_at,
            Candidate.notice_period_days, Candidate.availability_date, Candidate.ai_consent_state,
            Candidate.archived_at, Candidate.erased_at, Candidate.merged_into_id,
        ).where(Candidate.id.in_(chunk))  # fmt: skip
        if cond is not None:
            stmt = stmt.where(cond)
        for r in (await db.execute(stmt)).all():
            facts[r.id] = {
                "candidate_id": r.id, "full_name": r.full_name, "human_id": r.human_id, "current_title": r.current_title,
                "last_activity_at": r.updated_at, "notice_period_days": r.notice_period_days,
                "availability_date": r.availability_date, "ai_consent_state": r.ai_consent_state,
                "gone": r.archived_at is not None or r.erased_at is not None or r.merged_into_id is not None,
            }  # fmt: skip
    return facts, set(ids) - set(facts)


async def _pool_members(ctx: RequestContext, db: AsyncSession, pool_id: uuid.UUID) -> list[uuid.UUID]:
    from app.core.errors import DomainError
    from app.services import pools_service

    try:
        return await pools_service.member_ids(ctx, db, pool_id, limit=POOL_MEMBER_LIMIT)
    except DomainError:
        return []


async def _template_pools(ctx: RequestContext, db: AsyncSession, template_key: str) -> list[TalentPool]:
    from app.services.talent_pools import common

    if ctx.principal is None or not _principal(ctx).has("pool:read"):
        return []
    return list(
        (
            await db.execute(
                select(TalentPool).where(
                    TalentPool.template_key == template_key,
                    TalentPool.status == "active",
                    common.visible(common.principal_of(ctx)),
                )
            )
        ).scalars()
    )


async def contractors_ending_ids(db: AsyncSession, candidate_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Members (any viewer) of an active ``contractors_ending`` template Pool -- placed people about to roll off."""
    if not candidate_ids:
        return set()
    rows = await db.execute(
        select(TalentPoolMember.candidate_id)
        .join(TalentPool, TalentPool.id == TalentPoolMember.pool_id)
        .where(
            TalentPool.template_key == "contractors_ending",
            TalentPool.status == "active",
            TalentPoolMember.removed_at.is_(None),
            TalentPoolMember.candidate_id.in_(list(candidate_ids)),
        )
    )
    return set(rows.scalars())


async def placed_ids(db: AsyncSession, candidate_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    if not candidate_ids:
        return set()
    rows = await db.execute(
        select(Placement.candidate_id).where(
            Placement.candidate_id.in_(list(candidate_ids)), Placement.status.in_(ACTIVE_PLACEMENT_STATUSES)
        )
    )
    return set(rows.scalars())


def _add_source(
    sources: dict[uuid.UUID, list[dict[str, Any]]],
    cid: uuid.UUID,
    kind: str,
    ref: Any = None,
    label: str | None = None,
) -> None:
    items = sources.setdefault(cid, [])
    entry = {
        "kind": kind,
        "ref": str(ref) if ref is not None else None,
        "label": (label or None) and str(label)[:120],
    }
    if not any(s["kind"] == entry["kind"] and s["ref"] == entry["ref"] for s in items):
        items.append(entry)


async def build_candidate_set(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    include_ids: Sequence[uuid.UUID] = (),
    source_tags: Mapping[uuid.UUID, Sequence[Mapping[str, Any]]] | None = None,
    notes: Mapping[uuid.UUID, str] | None = None,
    include_silver_medalists: bool = True,
) -> CandidateSet:
    """See the module docstring. ``rows`` are dicts ready for ``ranking.rank`` (``candidate_id``, ``overall``,
    ``last_activity_at``, ``sources``, ``match_id``, ``explanation``, ``flags``, ``note`` + display facts)."""
    from app.services import pipeline_service
    from app.services.ai import gateway as ai_gateway
    from app.services.matching import service as matching
    from app.services.talent_pools import rediscovery as pool_rediscovery

    sources: dict[uuid.UUID, list[dict[str, Any]]] = {}
    scores: dict[uuid.UUID, dict[str, Any]] = {}
    # 1. the deterministic P1 match list (404 outside the caller's Demand scope)
    listing = await matching.list_matches(ctx, db, demand_id, page_size=MATCH_PAGE)
    for m in listing.items:
        cid = m.candidate.id
        _add_source(sources, cid, "match", m.id, "Match")
        scores[cid] = {"overall": m.overall, "match_id": m.id, "explanation": m.explanation}
    # 2. pinned Pools + the rediscovery pools section
    principal = _principal(ctx)
    if principal.has("pool:read"):
        for pin in await pool_rediscovery.list_pins(ctx, db, demand_id):
            for cid in await _pool_members(ctx, db, pin.pool_id):
                _add_source(sources, cid, "pool", pin.pool_id, pin.name)
        section = await pool_rediscovery.pools_section(ctx, db, demand_id) or {}
        for pool in section.get("pools") or []:
            for member in pool.get("top") or []:
                _add_source(
                    sources,
                    uuid.UUID(str(member["candidate_id"])),
                    "pool",
                    pool.get("pool_id"),
                    pool.get("name"),
                )
        # 3. silver medalists
        if include_silver_medalists:
            for pool in await _template_pools(ctx, db, "silver_medalists"):
                for cid in await _pool_members(ctx, db, pool.id):
                    _add_source(sources, cid, "silver_medalist", pool.id, "Silver medalist")
    # 4. ids found by the agent (keyword / structured search, rediscovery) with their tags
    searched: set[uuid.UUID] = set()
    tags = dict(source_tags or {})
    for cid in include_ids:
        given = tags.get(cid) or [{"kind": "search"}]
        for tag in given:
            kind = str(tag.get("kind") or "search")
            if kind == "search":
                searched.add(cid)
            if kind in ("search", "rediscovery") or cid not in sources:
                _add_source(
                    sources,
                    cid,
                    kind
                    if kind in ("search", "rediscovery", "pool", "silver_medalist", "match")
                    else "search",
                    tag.get("ref"),
                    tag.get("label"),
                )
    all_ids = list(sources)
    facts, unknown = await candidate_facts(ctx, db, all_ids)
    dropped_not_found = sorted((set(include_ids) & unknown), key=str)
    excluded: dict[str, int] = {
        "in_pipeline": 0,
        "do_not_contact": 0,
        "placed": 0,
        "archived": 0,
        "no_ai_consent": 0,
    }
    visible = [cid for cid in all_ids if cid in facts]
    # scores of non-match ids (read-only stored rows)
    missing = [cid for cid in visible if cid not in scores]
    if missing:
        for m in await matching.scores_for_candidates(ctx, db, demand_id, missing):
            scores[m.candidate.id] = {"overall": m.overall, "match_id": m.id, "explanation": m.explanation}
    in_pipeline = await pipeline_service.candidate_ids_for_demand(ctx, db, demand_id)
    gone = {cid for cid in visible if facts[cid]["gone"]}
    candidates_left = [cid for cid in visible if cid not in gone and cid not in in_pipeline]
    placed = await placed_ids(db, candidates_left)
    if placed:
        placed -= await contractors_ending_ids(db, sorted(placed, key=str))
    from app.services.sourcing_agent.contactability import do_not_contact_ids

    dnc = await do_not_contact_ids(db, candidates_left)
    consent_required = (await ai_gateway.processing_basis(db)) == "consent_required"
    rows: list[dict[str, Any]] = []
    note_map = dict(notes or {})
    for cid in visible:
        if cid in gone:
            excluded["archived"] += 1
            continue
        if cid in in_pipeline:
            excluded["in_pipeline"] += 1
            continue
        if cid in dnc:
            excluded["do_not_contact"] += 1
            continue
        if cid in placed:
            excluded["placed"] += 1
            continue
        flags: list[str] = []
        score = scores.get(cid) or {}
        explanation = score.get("explanation")
        if consent_required and facts[cid]["ai_consent_state"] not in CONSENTED_STATES:
            if cid not in searched:
                excluded["no_ai_consent"] += 1
                continue
            flags.append("no_ai_consent")
            explanation = None
        f = facts[cid]
        rows.append(
            {
                "candidate_id": cid,
                "overall": score.get("overall"),
                "match_id": score.get("match_id"),
                "explanation": (explanation or None) and str(explanation)[:400],
                "sources": sources[cid],
                "flags": flags,
                "note": (note_map.get(cid) or None),
                "last_activity_at": f["last_activity_at"],
                "notice_period_days": f["notice_period_days"],
                "availability_date": f["availability_date"],
                "full_name": f["full_name"],
                "human_id": f["human_id"],
                "current_title": f["current_title"],
            }
        )
    return CandidateSet(
        demand_id=demand_id, rows=rows, considered_count=len(all_ids), excluded_counts=excluded,
        dropped_not_found=dropped_not_found,
    )  # fmt: skip


# ================================================================ engagement facts (drafting)
async def engagement(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, demand_id: uuid.UUID
) -> dict[str, Any]:
    from sqlalchemy import func

    from app.models.comms import Email, EmailRecipient
    from app.models.pipeline import Application
    from app.models.sourcing_agent import AgentShortlistEntry

    facts, _ = await candidate_facts(ctx, db, [candidate_id])
    if candidate_id not in facts:
        raise NotFound("Candidate not found")
    info = await demand_facts(ctx, db, demand_id)
    last_email = (
        await db.execute(
            select(func.max(Email.created_at))
            .join(EmailRecipient, EmailRecipient.message_id == Email.id)
            .where(EmailRecipient.entity_type == "candidate", EmailRecipient.entity_id == candidate_id)
        )
    ).scalar_one_or_none()
    last_outreach = (
        await db.execute(
            select(func.max(AgentShortlistEntry.outreach_sent_at)).where(
                AgentShortlistEntry.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()
    contacted = max((d for d in (last_email, last_outreach) if d is not None), default=None)
    open_apps = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Application)
                .where(Application.candidate_id == candidate_id, Application.archived_at.is_(None))
            )
        ).scalar_one()
    )
    f = facts[candidate_id]
    return {
        "last_activity_at": f["last_activity_at"], "last_contacted_at": contacted, "open_applications": open_apps,
        "notice_period_days": f["notice_period_days"], "availability_date": f["availability_date"],
        "posting_url": await posting_url(db, info["posting"]),
    }  # fmt: skip


async def agent_enabled(db: AsyncSession) -> bool:
    """Tell whether the Sourcing Agent is on: the ``agentic`` master switch and ``agent_sourcing`` (no kill switch)."""
    from app.services.agent_runtime_service import agentic_state
    from app.services.ai import gateway as ai_gateway

    features = await ai_gateway.features_map(db)
    if not (features.get("agentic") and features.get("agent_sourcing")):
        return False
    state = await agentic_state(RequestContext(), db)
    return not (state.kill_switch or state.platform_kill_switch or state.global_kill_switch)


def now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "agent_enabled",
    "build_candidate_set",
    "candidate_facts",
    "contractors_ending_ids",
    "demand_facts",
    "engagement",
    "get_settings_",
    "placed_ids",
    "posting_url",
    "settings_out",
    "settings_row",
    "update_settings",
]
