"""Thin read functions the candidates service offers to search / ai / matching (Epic 5).

* ``get_search_projection(db, ids)`` -- flat rows for the ``candidate_search_docs`` read model.
* ``get_matching_profiles(db, ids)`` -- ``CandidateMatchProfile`` (structured fields only: no name, contact
  data, resume text or protected attributes).
* ``resolve_skills(ctx, db, names)`` (no pending skills are created), ``skill_graph(db)``,
  ``ai_consent_active(db, ids)``, ``owner_names`` / ``tag_names`` (facet labels).

No permission checks: callers (search / matching services) scope results themselves. RLS confines every
query to the session's tenant.
"""

import dataclasses
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.candidates import (
    Candidate,
    CandidateCertification,
    CandidateEmail,
    CandidateEmployment,
    CandidatePhone,
    CandidateResume,
    CandidateSkill,
    CandidateTag,
    Skill,
    Tag,
    TenantSkill,
)
from app.schemas.matching_profiles import CandidateMatchProfile, CandidateSkillFact, SkillGraph, annualise
from app.services.candidates import skills as skills_svc
from app.services.candidates.common import normalize_skill, user_names

AI_CONSENT_ACTIVE_STATES = ("granted", "not_required", "pending")
RESUME_TEXT_MAX = 200_000


@dataclasses.dataclass(frozen=True, slots=True)
class SearchProjection:
    candidate_id: uuid.UUID
    human_id: str
    owner_id: uuid.UUID
    display_name: str
    email: str | None
    phone_e164: str | None
    headline: str | None
    current_employer: str | None
    current_title: str | None
    skill_ids: tuple[uuid.UUID, ...]
    skill_names: tuple[str, ...]
    certifications: tuple[str, ...]
    tags: tuple[uuid.UUID, ...]
    tag_names: tuple[str, ...]
    employers: tuple[str, ...]
    source: str
    country: str | None
    state: str | None
    city: str | None
    work_auth: tuple[str, ...]
    experience_years: Decimal | None
    expected_comp_amount: Decimal | None
    expected_comp_currency: str | None
    expected_comp_unit: str | None
    notice_days: int | None
    available_from: date | None
    ai_consent: bool
    is_archived: bool
    candidate_updated_at: datetime
    resume_text: str | None


def _loc(value: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(value, dict):
        return None, None, None
    return value.get("city") or None, value.get("state") or None, value.get("country") or None


def _years(months: int | None) -> Decimal | None:
    if months is None:
        return None
    return (Decimal(months) / Decimal(12)).quantize(Decimal("0.1"))


def _consent_active(state: str | None) -> bool:
    return state in AI_CONSENT_ACTIVE_STATES


def _split_work_auth(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = [p.strip() for p in value.replace(";", ",").split(",")]
    return tuple(p for p in parts if p)


async def _skill_rows(
    db: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[CandidateSkillFact]]:
    rows = list(
        (await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id.in_(ids)))).scalars()
    )
    gids = {r.skill_id for r in rows if r.skill_id}
    tids = {r.tenant_skill_id for r in rows if r.tenant_skill_id}
    names: dict[uuid.UUID, str] = {}
    if gids:
        names.update(
            {s.id: s.name for s in (await db.execute(select(Skill).where(Skill.id.in_(gids)))).scalars()}
        )
    if tids:
        for t in (await db.execute(select(TenantSkill).where(TenantSkill.id.in_(tids)))).scalars():
            if t.status != "rejected":
                names[t.id] = t.name
    out: dict[uuid.UUID, list[CandidateSkillFact]] = defaultdict(list)
    for r in rows:
        sid = r.skill_id or r.tenant_skill_id
        if sid is None or sid not in names:
            continue
        out[r.candidate_id].append(
            # Epic 37 (FR-190): proficiency feeds the matching proficiency factor.
            CandidateSkillFact(skill_id=sid, name=names[sid], years=r.years, proficiency=r.proficiency)
        )
    for facts in out.values():
        facts.sort(key=lambda f: (-(f.years or Decimal(0)), f.name.lower()))
    return out


async def _children(
    db: AsyncSession, model: Any, ids: Sequence[uuid.UUID], *order: Any
) -> dict[uuid.UUID, list[Any]]:
    out: dict[uuid.UUID, list[Any]] = defaultdict(list)
    stmt = select(model).where(model.candidate_id.in_(ids))
    if order:
        stmt = stmt.order_by(*order)
    for row in (await db.execute(stmt)).scalars():
        out[row.candidate_id].append(row)
    return out


async def get_search_projection(
    db: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, SearchProjection | None]:
    """Projection rows for ``ids``; ``None`` for ids that no longer exist or were erased (drop the doc)."""
    ids = list(dict.fromkeys(ids))
    out: dict[uuid.UUID, SearchProjection | None] = dict.fromkeys(ids)
    if not ids:
        return out
    cands = list((await db.execute(select(Candidate).where(Candidate.id.in_(ids)))).scalars())
    live = [c.id for c in cands if c.erased_at is None]
    skills = await _skill_rows(db, live)
    emails = await _children(
        db, CandidateEmail, live, CandidateEmail.is_primary.desc(), CandidateEmail.created_at
    )
    phones = await _children(
        db, CandidatePhone, live, CandidatePhone.is_primary.desc(), CandidatePhone.created_at
    )
    certs = await _children(db, CandidateCertification, live, CandidateCertification.created_at)
    jobs = await _children(
        db, CandidateEmployment, live, CandidateEmployment.is_current.desc(), CandidateEmployment.created_at
    )
    tag_rows: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = defaultdict(list)
    if live:
        for cid, tid, name in await db.execute(
            select(CandidateTag.candidate_id, Tag.id, Tag.name)
            .join(Tag, Tag.id == CandidateTag.tag_id)
            .where(CandidateTag.candidate_id.in_(live))
            .order_by(Tag.name)
        ):
            tag_rows[cid].append((tid, name))
    resumes: dict[uuid.UUID, str | None] = {}
    if live:
        for cid, txt in await db.execute(
            select(CandidateResume.candidate_id, CandidateResume.extracted_text)
            .where(CandidateResume.candidate_id.in_(live))
            .order_by(CandidateResume.candidate_id, CandidateResume.version.desc())
            .distinct(CandidateResume.candidate_id)
        ):
            resumes[cid] = (txt or "")[:RESUME_TEXT_MAX] or None
    for c in cands:
        if c.erased_at is not None:
            continue
        city, state, country = _loc(c.location)
        facts = skills.get(c.id, [])
        employers = tuple(dict.fromkeys(j.employer for j in jobs.get(c.id, []) if j.employer))
        headline = " · ".join(p for p in (c.current_title, c.current_employer) if p) or None
        out[c.id] = SearchProjection(
            candidate_id=c.id,
            human_id=c.human_id,
            owner_id=c.owner_id,
            display_name=c.full_name,
            email=(emails.get(c.id) or [None])[0] and emails[c.id][0].email,
            phone_e164=(phones.get(c.id) or [None])[0] and phones[c.id][0].phone_e164,
            headline=headline,
            current_employer=c.current_employer,
            current_title=c.current_title,
            skill_ids=tuple(f.skill_id for f in facts),
            skill_names=tuple(f.name for f in facts),
            certifications=tuple(x.name for x in certs.get(c.id, [])),
            tags=tuple(t for t, _ in tag_rows.get(c.id, [])),
            tag_names=tuple(n for _, n in tag_rows.get(c.id, [])),
            employers=employers,
            source=c.source,
            country=country,
            state=state,
            city=city,
            work_auth=_split_work_auth(c.work_authorization),
            experience_years=_years(c.total_experience_months),
            expected_comp_amount=c.expected_ctc_amount,
            expected_comp_currency=c.expected_ctc_currency,
            expected_comp_unit=c.expected_ctc_unit,
            notice_days=c.notice_period_days,
            available_from=c.availability_date,
            ai_consent=_consent_active(c.ai_consent_state),
            is_archived=c.archived_at is not None or c.merged_into_id is not None,
            candidate_updated_at=c.updated_at,
            resume_text=resumes.get(c.id),
        )
    return out


async def get_matching_profiles(db: AsyncSession, ids: Sequence[uuid.UUID]) -> list[CandidateMatchProfile]:
    """Structured matching inputs for live (not archived / merged / erased) candidates among ``ids``."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    cands = [
        c
        for c in (await db.execute(select(Candidate).where(Candidate.id.in_(ids)))).scalars()
        if c.erased_at is None and c.archived_at is None and c.merged_into_id is None
    ]
    live = [c.id for c in cands]
    skills = await _skill_rows(db, live)
    certs = await _children(db, CandidateCertification, live, CandidateCertification.created_at)
    jobs = await _children(
        db, CandidateEmployment, live, CandidateEmployment.is_current.desc(), CandidateEmployment.created_at
    )
    out: list[CandidateMatchProfile] = []
    for c in sorted(cands, key=lambda x: str(x.id)):
        city, state, country = _loc(c.location)
        preferred = tuple(
            str(p.get("city")) for p in (c.preferred_locations or []) if isinstance(p, dict) and p.get("city")
        )
        out.append(
            CandidateMatchProfile(
                candidate_id=c.id,
                current_title=c.current_title,
                skills=tuple(skills.get(c.id, [])),
                experience_years=_years(c.total_experience_months),
                city=city,
                state=state,
                country=country,
                preferred_cities=preferred,
                willing_to_relocate=c.willing_to_relocate,
                work_auth=_split_work_auth(c.work_authorization),
                expected_comp_annual=annualise(c.expected_ctc_amount, c.expected_ctc_unit),
                expected_comp_currency=c.expected_ctc_currency,
                available_from=c.availability_date,
                notice_days=c.notice_period_days,
                domains=(),
                role_titles=tuple(dict.fromkeys(j.title for j in jobs.get(c.id, []) if j.title)),
                employers=tuple(dict.fromkeys(j.employer for j in jobs.get(c.id, []) if j.employer)),
                certifications=tuple(x.name for x in certs.get(c.id, [])),
                graduation_year=None,
            )
        )
    return out


async def resolve_skills(
    ctx: RequestContext, db: AsyncSession, names: Iterable[str]
) -> dict[str, uuid.UUID | None]:
    """Map free-text skill names to catalog skill ids (unknown -> None; never creates pending skills)."""
    out: dict[str, uuid.UUID | None] = {}
    for name in names:
        if name in out:
            continue
        ref = await skills_svc.resolve_skill(ctx, db, name, create_pending=False)
        out[name] = ref.id if ref is not None and ref.status != "rejected" else None
    return out


async def skill_graph(db: AsyncSession) -> SkillGraph:
    parents: dict[uuid.UUID, uuid.UUID] = {}
    for sid, parent in await db.execute(
        select(Skill.id, Skill.parent_id).where(Skill.parent_id.is_not(None))
    ):
        parents[sid] = parent
    for sid, parent in await db.execute(
        select(TenantSkill.id, TenantSkill.parent_skill_id).where(TenantSkill.parent_skill_id.is_not(None))
    ):
        parents[sid] = parent
    return SkillGraph(parents=parents)


async def skill_names(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    wanted = set(ids)
    if not wanted:
        return {}
    out = {s.id: s.name for s in (await db.execute(select(Skill).where(Skill.id.in_(wanted)))).scalars()}
    out.update(
        {
            t.id: t.name
            for t in (await db.execute(select(TenantSkill).where(TenantSkill.id.in_(wanted)))).scalars()
        }
    )
    return out


async def ai_consent_active(db: AsyncSession, ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    wanted = list(set(ids))
    if not wanted:
        return set()
    rows = await db.execute(
        select(Candidate.id).where(
            Candidate.id.in_(wanted),
            Candidate.ai_consent_state.in_(AI_CONSENT_ACTIVE_STATES),
            Candidate.erased_at.is_(None),
        )
    )
    return {r[0] for r in rows}


async def live_ids(db: AsyncSession, *, after: uuid.UUID | None = None, limit: int = 1000) -> list[uuid.UUID]:
    """Keyset-paged ids of every non-erased candidate (reindex / backfill)."""
    stmt = select(Candidate.id).where(Candidate.erased_at.is_(None)).order_by(Candidate.id).limit(limit)
    if after is not None:
        stmt = stmt.where(Candidate.id > after)
    return [r[0] for r in await db.execute(stmt)]


async def owner_names(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    return await user_names(db, set(ids))


async def tag_names(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    wanted = set(ids)
    if not wanted:
        return {}
    return {t.id: str(t.name) for t in (await db.execute(select(Tag).where(Tag.id.in_(wanted)))).scalars()}


def normalize_skill_text(text: str) -> str:
    return normalize_skill(text)
