"""JD versions, AI generation, bias check and publishing (FR-14, FR-16, story 4.3).

* Every save creates a new version (``version_no`` per demand); restore copies into a new version.
* ``generate_jd`` goes through the AI gateway (AD-6); compensation is sent only for a posting with
  ``show_rate``. AI unavailable -> 409 ``ai_unavailable`` and no version.
* ``publish`` needs an Open demand, a ``full``/``posting`` version and acknowledged bias flags; exactly one
  version is published (``demands.published_jd_version_id``). A demand leaving Open is unpublished by
  ``lifecycle.transition_demand``.
"""

import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.events.demands import DemandPublished, DemandUnpublished
from app.models.demands import BiasedTerm, Demand, DemandPosting, JdVersion
from app.schemas.demands import (
    BiasCheckOut,
    BiasedTermIn,
    BiasedTermOut,
    BiasMatchOut,
    JdDraft,
    JdGenerateIn,
    JdVersionIn,
    JdVersionOut,
    LivePosting,
    PostingIn,
    PostingOut,
)
from app.services.demands import bias
from app.services.demands.common import READ, UPDATE, actor_kind_of, get_for, now, timeline
from app.services.demands.transitions import ActorKind

PUBLISHABLE_KINDS = frozenset({"full", "posting"})
_COMPENSATION_INPUTS = ("bill_rate", "pay_rate_min", "pay_rate_max", "currency", "rate_unit")


def ai_unavailable(error_code: str | None = None) -> ConflictError:
    return ConflictError(
        "AI is unavailable right now; continue manually",
        code="ai_unavailable",
        details={"reason": error_code} if error_code else None,
    )


def _human_only() -> Forbidden:
    return Forbidden("Only a person can perform this action", code="human_only_action")


def _version_out(row: JdVersion, published_id: uuid.UUID | None) -> JdVersionOut:
    out = JdVersionOut.model_validate(row)
    out.is_published = row.id == published_id
    return out


async def _next_no(db: AsyncSession, demand_id: uuid.UUID) -> int:
    current = (
        await db.execute(select(func.max(JdVersion.version_no)).where(JdVersion.demand_id == demand_id))
    ).scalar_one()
    return int(current or 0) + 1


async def _add_version(
    ctx: RequestContext,
    db: AsyncSession,
    demand: Demand,
    *,
    kind: str,
    content: str,
    author_type: str,
    ai_action_id: uuid.UUID | None = None,
    restored_from_id: uuid.UUID | None = None,
) -> JdVersion:
    row = JdVersion(
        id=uuid.uuid4(),
        tenant_id=demand.tenant_id,
        demand_id=demand.id,
        version_no=await _next_no(db, demand.id),
        kind=kind,
        content=content,
        author_type=author_type,
        author_id=getattr(ctx.principal, "id", None),
        ai_action_id=ai_action_id,
        restored_from_id=restored_from_id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_jd_versions(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[JdVersionOut]:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    rows = (
        await db.execute(
            select(JdVersion).where(JdVersion.demand_id == demand.id).order_by(JdVersion.version_no.desc())
        )
    ).scalars()
    return [_version_out(r, demand.published_jd_version_id) for r in rows]


async def save_jd_version(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: JdVersionIn
) -> JdVersionOut:
    demand = await get_for(ctx, db, demand_id, UPDATE)
    row = await _add_version(ctx, db, demand, kind=data.kind, content=data.content, author_type="user")
    return _version_out(row, demand.published_jd_version_id)


def _generation_inputs(demand: Demand, skills: list[dict[str, Any]], data: JdGenerateIn) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "title": demand.title,
        "end_client": demand.end_client,
        "positions": demand.positions,
        "department": demand.department,
        "employment_type": demand.employment_type,
        "country": demand.country,
        "location": demand.location,
        "work_mode": demand.work_mode,
        "min_experience_years": demand.min_experience_years,
        "max_experience_years": demand.max_experience_years,
        "education": demand.education,
        "certifications": demand.certifications,
        "work_authorization": demand.work_authorization,
        "clearance": demand.clearance,
        "languages": demand.languages,
        "shift": demand.shift,
        "travel_percent": demand.travel_percent,
        "start_date": demand.start_date,
        "skills": skills,
        "interview_plan": demand.interview_plan,
    }
    if data.kind == "posting" and data.show_rate:
        fields.update({k: getattr(demand, k) for k in _COMPENSATION_INPUTS})
    return fields


async def generate_jd(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: JdGenerateIn
) -> JdVersionOut:
    from app.models.demands import DemandSkill
    from app.services import ai

    demand = await get_for(ctx, db, demand_id, UPDATE)
    skills = [
        {"label": s.skill_label, "kind": s.kind, "min_years": s.min_years}
        for s in (
            await db.execute(
                select(DemandSkill).where(DemandSkill.demand_id == demand.id).order_by(DemandSkill.position)
            )
        ).scalars()
    ]
    fields = _generation_inputs(demand, skills, data)
    result = await ai.run(
        ctx,
        db,
        "jd_generation",
        "demands_jd_generate.v1",  # Epic 55: pinned -- the Demand Agent uses v2 (tenant template); P1 is unchanged
        inputs={"kind": data.kind},
        untrusted={"demand_fields": json.dumps(fields, default=str, ensure_ascii=False, indent=1)},
        output_schema=JdDraft,
        subject=("demand", demand.id),
        input_ref=f"demand:{demand.id}",
    )
    if not result.ok or not isinstance(result.data, JdDraft):
        raise ai_unavailable(result.error_code)
    row = await _add_version(
        ctx,
        db,
        demand,
        kind=data.kind,
        content=result.data.content,
        author_type="ai",
        ai_action_id=result.action_id,
    )
    await timeline(ctx, db, demand.id, f"JD generated by AI (version {row.version_no}, {data.kind})")
    return _version_out(row, demand.published_jd_version_id)


async def _version(db: AsyncSession, demand: Demand, version_id: uuid.UUID) -> JdVersion:
    row = await db.get(JdVersion, version_id)
    if row is None or row.demand_id != demand.id:
        raise NotFound("JD version not found")
    return row


async def restore_jd_version(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, version_id: uuid.UUID
) -> JdVersionOut:
    demand = await get_for(ctx, db, demand_id, UPDATE)
    source = await _version(db, demand, version_id)
    row = await _add_version(
        ctx,
        db,
        demand,
        kind=source.kind,
        content=source.content,
        author_type="user",
        restored_from_id=source.id,
    )
    return _version_out(row, demand.published_jd_version_id)


# ================================================================ bias
async def effective_terms(db: AsyncSession) -> list[tuple[str, str]]:
    rows = (await db.execute(select(BiasedTerm.term, BiasedTerm.category, BiasedTerm.enabled))).all()
    return bias.effective_terms((str(t), c, e) for t, c, e in rows)


async def bias_matches(db: AsyncSession, text: str) -> list[BiasMatchOut]:
    return [BiasMatchOut(**m) for m in bias.check(text, await effective_terms(db))]


async def bias_check(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, version_id: uuid.UUID
) -> BiasCheckOut:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    row = await _version(db, demand, version_id)
    return BiasCheckOut(matches=await bias_matches(db, row.content))


async def list_biased_terms(ctx: RequestContext, db: AsyncSession) -> list[BiasedTermOut]:  # ruff: ignore[unused-function-argument]
    rows = {str(r.term).lower(): r for r in (await db.execute(select(BiasedTerm))).scalars()}
    out: list[BiasedTermOut] = []
    for term, category in bias.DEFAULT_BIASED_TERMS:
        row = rows.pop(term.lower(), None)
        out.append(
            BiasedTermOut(
                id=row.id if row else None,
                term=term,
                category=row.category if row else category,
                enabled=row.enabled if row else True,
                source="default",
            )
        )
    out.extend(
        BiasedTermOut(id=r.id, term=str(r.term), category=r.category, enabled=r.enabled, source="tenant")
        for r in sorted(rows.values(), key=lambda r: str(r.term).lower())
    )
    return out


async def upsert_biased_term(ctx: RequestContext, db: AsyncSession, data: BiasedTermIn) -> BiasedTermOut:
    term = " ".join(data.term.split())
    row = (await db.execute(select(BiasedTerm).where(BiasedTerm.term == term))).scalar_one_or_none()
    if row is None:
        row = BiasedTerm(id=uuid.uuid4(), tenant_id=ctx.tenant_id, term=term, category=data.category)
        db.add(row)
    row.category = data.category
    row.enabled = data.enabled
    await db.flush()
    is_default = term.lower() in {t.lower() for t, _ in bias.DEFAULT_BIASED_TERMS}
    return BiasedTermOut(
        id=row.id,
        term=term,
        category=row.category,
        enabled=row.enabled,
        source="default" if is_default else "tenant",
    )


async def delete_biased_term(ctx: RequestContext, db: AsyncSession, term_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    row = await db.get(BiasedTerm, term_id)
    if row is None:
        raise NotFound("Term not found")
    await db.delete(row)
    await db.flush()


# ================================================================ publishing
async def get_posting(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> PostingOut | None:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    row = (
        await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))
    ).scalar_one_or_none()
    return PostingOut.model_validate(row) if row else None


PUBLISHING_ACTORS = frozenset({"user", "app", "api_key"})


async def publish(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    data: PostingIn,
    *,
    actor_kind: ActorKind | None = None,
) -> PostingOut:
    # FR-65: never ai / workflow / system. Epic 67 (FR-346): the Public API (app / api_key) may publish to the
    # careers site; an Open, approved demand is still required.
    if (actor_kind or actor_kind_of(ctx)) not in PUBLISHING_ACTORS:
        raise _human_only()
    demand = await get_for(ctx, db, demand_id, UPDATE)
    if demand.status != "open":
        raise ConflictError("Only an Open demand can be published", code="demand_not_open")
    version = await _version(db, demand, data.jd_version_id)
    if version.kind not in PUBLISHABLE_KINDS:
        raise ValidationFailed(
            "Only a full JD or posting text can be published",
            code="invalid_jd_kind",
            details={"allowed": sorted(PUBLISHABLE_KINDS)},
        )
    matches = await bias_matches(db, version.content)
    if matches and not data.acknowledge_bias_flags:
        raise ConflictError(
            "The JD contains flagged wording; review or acknowledge it",
            code="bias_flags_unacknowledged",
            details={"matches": [m.model_dump() for m in matches]},
        )
    posting = (
        await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))
    ).scalar_one_or_none()
    if posting is None:
        posting = DemandPosting(id=uuid.uuid4(), tenant_id=demand.tenant_id, demand_id=demand.id)
        db.add(posting)
    posting.jd_version_id = version.id
    posting.show_rate = data.show_rate
    posting.show_location = data.show_location
    posting.show_client_name = data.show_client_name
    posting.is_live = True
    posting.published_at = now()
    posting.unpublished_at = None
    demand.published_jd_version_id = version.id
    await db.flush()
    emit(db, DemandPublished(tenant_id=demand.tenant_id, demand_id=demand.id, jd_version_id=version.id))
    await timeline(ctx, db, demand.id, f"Published JD version {version.version_no}")
    await db.refresh(posting)
    return PostingOut.model_validate(posting)


async def unpublish_demand(ctx: RequestContext, db: AsyncSession, demand: Demand, actor_kind: str) -> bool:
    """Take the posting down (no-op when not live). Used by the API and on leaving Open (actor system)."""
    posting = (
        await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))
    ).scalar_one_or_none()
    if posting is None or not posting.is_live:
        return False
    posting.is_live = False
    posting.unpublished_at = now()
    demand.published_jd_version_id = None
    await db.flush()
    emit(db, DemandUnpublished(tenant_id=demand.tenant_id, demand_id=demand.id, actor_kind=actor_kind))
    await timeline(ctx, db, demand.id, "Posting unpublished", actor_kind=actor_kind)
    return True


async def unpublish(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, *, actor_kind: ActorKind | None = None
) -> PostingOut | None:
    kind = actor_kind or actor_kind_of(ctx)
    if kind not in PUBLISHING_ACTORS:
        raise _human_only()
    demand = await get_for(ctx, db, demand_id, UPDATE)
    await unpublish_demand(ctx, db, demand, kind)
    row = (
        await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("The demand has no posting")
    await db.refresh(row)
    return PostingOut.model_validate(row)


async def list_live_postings(db: AsyncSession) -> list[LivePosting]:
    """Live postings of the session's tenant (Career Portal, Epic 7)."""
    from app.models.clients import Client

    rows = (
        await db.execute(
            select(DemandPosting, Demand, JdVersion, Client.name)
            .join(Demand, Demand.id == DemandPosting.demand_id)
            .join(JdVersion, JdVersion.id == DemandPosting.jd_version_id)
            .join(Client, Client.id == Demand.client_id)
            # Epic 36 (AD-50): internal-only demands never reach the public careers surfaces.
            .where(DemandPosting.is_live.is_(True), Demand.status == "open", Demand.visibility != "internal")
            .order_by(DemandPosting.published_at.desc())
        )
    ).all()
    return [
        LivePosting(
            demand_id=d.id,
            req_id=d.req_id,
            title=d.title,
            content=v.content,
            location=d.location if p.show_location else None,
            country=d.country if p.show_location else None,
            work_mode=d.work_mode,
            employment_type=d.employment_type,
            client_name=str(client_name) if p.show_client_name else None,
            bill_rate=d.bill_rate if p.show_rate else None,
            currency=d.currency if p.show_rate else None,
            rate_unit=d.rate_unit if p.show_rate else None,
            published_at=p.published_at,
        )
        for p, d, v, client_name in rows
    ]
