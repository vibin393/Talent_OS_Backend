"""Duplicate detection (FR-21, story 3.5).

* **Exact:** a shared email, E.164 phone or normalised profile URL.
* **Fuzzy:** ``similarity(full_name) >= 0.6`` AND (``similarity(current_employer) >= 0.5`` OR the same
  ``location.city``, case-insensitive).

Runs on create, update, import and resume apply; inserts *open* review items for the ordered pair
``(least, greatest)`` and never merges on its own. A pair once kept separate (or merged) is never re-flagged.
"""

import dataclasses
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.pagination import Page, PageParams
from app.models.candidates import Candidate, CandidateDuplicate, CandidateEmail, CandidateLink, CandidatePhone
from app.schemas.candidates import CandidateOut, DuplicateDetailOut, DuplicateOut
from app.services.candidates import records
from app.services.candidates.common import now, principal_of, read_condition

NAME_THRESHOLD = 0.6
EMPLOYER_THRESHOLD = 0.5

COMPARE_FIELDS = (
    "full_name",
    "current_title",
    "current_employer",
    "location",
    "total_experience_months",
    "current_ctc_amount",
    "current_ctc_currency",
    "current_ctc_unit",
    "expected_ctc_amount",
    "expected_ctc_currency",
    "expected_ctc_unit",
    "availability_date",
    "notice_period_days",
    "last_working_date",
    "work_authorization",
    "willing_to_relocate",
    "preferred_locations",
    "source",
    "source_detail",
    "owner_id",
)


@dataclasses.dataclass(frozen=True, slots=True)
class DuplicateHit:
    duplicate_id: uuid.UUID
    other_id: uuid.UUID
    reasons: tuple[str, ...]
    score: Decimal


def _pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if a < b else (b, a)


@dataclasses.dataclass(frozen=True, slots=True)
class Probe:
    """The identifying values of a (possibly not yet saved) candidate."""

    full_name: str | None
    current_employer: str | None = None
    city: str | None = None
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    exclude_id: uuid.UUID | None = None


async def probe_of(db: AsyncSession, cand: Candidate) -> Probe:
    async def _vals(col: Any, fk: Any) -> tuple[str, ...]:
        return tuple((await db.execute(select(col).where(fk == cand.id))).scalars())

    city = cand.location.get("city") if isinstance(cand.location, dict) else None
    return Probe(
        full_name=cand.full_name,
        current_employer=cand.current_employer,
        city=city,
        emails=await _vals(CandidateEmail.email, CandidateEmail.candidate_id),
        phones=await _vals(CandidatePhone.phone_e164, CandidatePhone.candidate_id),
        links=await _vals(CandidateLink.url_norm, CandidateLink.candidate_id),
        exclude_id=cand.id,
    )


def _live(exclude_id: uuid.UUID | None) -> Any:
    cond = Candidate.merged_into_id.is_(None)
    return and_(cond, Candidate.id != exclude_id) if exclude_id is not None else cond


async def _exact_matches(db: AsyncSession, probe: Probe) -> dict[uuid.UUID, set[str]]:
    found: dict[uuid.UUID, set[str]] = {}
    checks: list[tuple[str, Any, Any, tuple[str, ...]]] = [
        ("exact_email", CandidateEmail, CandidateEmail.email, probe.emails),
        ("exact_phone", CandidatePhone, CandidatePhone.phone_e164, probe.phones),
        ("exact_link", CandidateLink, CandidateLink.url_norm, probe.links),
    ]
    for reason, model, col, values in checks:
        if not values:
            continue
        stmt = (
            select(model.candidate_id)
            .join(Candidate, Candidate.id == model.candidate_id)
            .where(col.in_(values), _live(probe.exclude_id))
            .distinct()
        )
        for (cid,) in await db.execute(stmt):
            found.setdefault(cid, set()).add(reason)
    return found


async def _fuzzy_matches(db: AsyncSession, probe: Probe) -> dict[uuid.UUID, tuple[set[str], float]]:
    if not probe.full_name:
        return {}
    name_sim = func.similarity(Candidate.full_name, literal(probe.full_name))
    employer = (probe.current_employer or "").strip()
    city = (probe.city or "").strip().lower()
    emp_sim = func.similarity(func.coalesce(Candidate.current_employer, ""), literal(employer))
    city_eq = func.coalesce(func.lower(Candidate.location["city"].astext) == literal(city), False)
    conditions = []
    if employer:
        conditions.append(emp_sim >= EMPLOYER_THRESHOLD)
    if city:
        conditions.append(city_eq)
    if not conditions:
        return {}
    stmt = select(Candidate.id, name_sim, emp_sim, city_eq).where(
        _live(probe.exclude_id), name_sim >= NAME_THRESHOLD, or_(*conditions)
    )
    out: dict[uuid.UUID, tuple[set[str], float]] = {}
    for cid, nsim, esim, same_city in await db.execute(stmt):
        reasons: set[str] = set()
        if employer and esim is not None and float(esim) >= EMPLOYER_THRESHOLD:
            reasons.add("fuzzy_name_employer")
        if city and same_city:
            reasons.add("fuzzy_name_city")
        out[cid] = (reasons, float(nsim))
    return out


async def probe_matches(db: AsyncSession, probe: Probe) -> dict[uuid.UUID, tuple[set[str], float]]:
    """``candidate_id -> (reasons, score)`` for every live candidate matching ``probe``."""
    exact = await _exact_matches(db, probe)
    fuzzy = await _fuzzy_matches(db, probe)
    out: dict[uuid.UUID, tuple[set[str], float]] = {}
    for other_id in sorted(set(exact) | set(fuzzy)):
        reasons = set(exact.get(other_id, set()))
        score = 1.0 if reasons else 0.0
        if other_id in fuzzy:
            reasons |= fuzzy[other_id][0]
            score = max(score, fuzzy[other_id][1])
        if reasons:
            out[other_id] = (reasons, score)
    return out


async def _get_pair_row(db: AsyncSession, a: uuid.UUID, b: uuid.UUID) -> CandidateDuplicate | None:
    stmt = select(CandidateDuplicate).where(
        CandidateDuplicate.candidate_a_id == a, CandidateDuplicate.candidate_b_id == b
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_duplicates(ctx: RequestContext, db: AsyncSession, cand: Candidate) -> list[DuplicateHit]:
    """Detect possible duplicates of ``cand`` and upsert open review items. Returns the open hits."""
    if cand.merged_into_id is not None:
        return []
    await db.flush()
    matches = await probe_matches(db, await probe_of(db, cand))
    hits: list[DuplicateHit] = []
    for other_id, (reasons, score) in matches.items():
        a, b = _pair(cand.id, other_id)
        new_score = Decimal(str(round(score, 3)))
        row = await _get_pair_row(db, a, b)
        if row is None:
            candidate_row = CandidateDuplicate(
                id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                candidate_a_id=a,
                candidate_b_id=b,
                reasons=sorted(reasons),
                score=new_score,
                status="open",
            )
            try:
                async with db.begin_nested():
                    db.add(candidate_row)
                    await db.flush()
                row = candidate_row
            except IntegrityError:  # flagged concurrently
                row = await _get_pair_row(db, a, b)
                if row is None:
                    continue
        else:
            if row.status != "open":
                continue  # kept separate / merged pairs are never re-flagged
            merged = sorted(set(row.reasons or []) | reasons)
            if merged != list(row.reasons or []):
                row.reasons = merged
            if new_score > row.score:
                row.score = new_score
        hits.append(DuplicateHit(row.id, other_id, tuple(row.reasons), row.score))
    await db.flush()
    return hits


async def link_by_exact_email(ctx: RequestContext, db: AsyncSession, email: str) -> uuid.UUID | None:  # ruff: ignore[unused-function-argument]
    """Existing candidate with exactly this email (Epic 9 applications link to it). Oldest live one wins."""
    from app.services.candidates.common import normalize_email

    norm = normalize_email(email)
    if norm is None:
        return None
    stmt = (
        select(Candidate.id)
        .join(CandidateEmail, CandidateEmail.candidate_id == Candidate.id)
        .where(CandidateEmail.email == norm, Candidate.merged_into_id.is_(None))
        .order_by(Candidate.archived_at.is_not(None), Candidate.created_at, Candidate.id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def visible_summaries(
    ctx: RequestContext, db: AsyncSession, ids: Iterable[uuid.UUID]
) -> list[CandidateOut]:
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    stmt = select(Candidate).where(Candidate.id.in_(wanted))
    cond = read_condition(principal_of(ctx))
    if cond is not None:
        stmt = stmt.where(cond)
    rows = {r.id: r for r in (await db.execute(stmt)).scalars()}
    return await records.summaries(db, [rows[i] for i in wanted if i in rows])


# ================================================================ queue API
def _visible_pairs(ctx: RequestContext) -> Any:
    principal = principal_of(ctx)
    a = aliased(Candidate)
    b = aliased(Candidate)
    stmt = (
        select(CandidateDuplicate, a, b)
        .join(a, a.id == CandidateDuplicate.candidate_a_id)
        .join(b, b.id == CandidateDuplicate.candidate_b_id)
    )
    from app.core.permissions import scope_condition

    for alias in (a, b):
        cond = scope_condition(principal, "candidate:read", alias.owner_id)
        if cond is not None:
            stmt = stmt.where(cond)
    return stmt


def _dup_out(row: CandidateDuplicate, a: CandidateOut, b: CandidateOut) -> DuplicateOut:
    return DuplicateOut(
        id=row.id,
        candidate_a=a,
        candidate_b=b,
        reasons=list(row.reasons or []),
        score=row.score,
        status=row.status,
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        created_at=row.created_at,
    )


async def list_duplicates(
    ctx: RequestContext, db: AsyncSession, status: str | None, params: PageParams
) -> Page[DuplicateOut]:
    stmt = _visible_pairs(ctx)
    if status:
        stmt = stmt.where(CandidateDuplicate.status == status)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(CandidateDuplicate.created_at.desc(), CandidateDuplicate.id)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).all()
    cands = [c for _, a, b in rows for c in (a, b)]
    outs = {o.id: o for o in await records.summaries(db, cands)}
    items = [_dup_out(d, outs[a.id], outs[b.id]) for d, a, b in rows]
    return Page[DuplicateOut](items=items, total=total, page=params.page, page_size=params.page_size)


async def count_open(ctx: RequestContext, db: AsyncSession) -> int:
    stmt = _visible_pairs(ctx).where(CandidateDuplicate.status == "open")
    return int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())


async def get_pair(
    ctx: RequestContext, db: AsyncSession, duplicate_id: uuid.UUID, *, for_update: bool = False
) -> tuple[CandidateDuplicate, Candidate, Candidate]:
    stmt = _visible_pairs(ctx).where(CandidateDuplicate.id == duplicate_id)
    if for_update:
        stmt = stmt.with_for_update(of=CandidateDuplicate)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise NotFound("Duplicate not found")
    return row[0], row[1], row[2]


async def get_duplicate(ctx: RequestContext, db: AsyncSession, duplicate_id: uuid.UUID) -> DuplicateDetailOut:
    dup, a, b = await get_pair(ctx, db, duplicate_id)
    sa, sb = await records.summaries(db, [a, b])
    pa = await records.build_360(ctx, db, a)
    pb = await records.build_360(ctx, db, b)
    differing = [f for f in COMPARE_FIELDS if pa.get(f) != pb.get(f)]
    base = _dup_out(dup, sa, sb)
    return DuplicateDetailOut(**base.model_dump(), profile_a=pa, profile_b=pb, differing_fields=differing)


async def keep_separate(ctx: RequestContext, db: AsyncSession, duplicate_id: uuid.UUID) -> DuplicateOut:
    dup, a, b = await get_pair(ctx, db, duplicate_id, for_update=True)
    if dup.status == "merged":
        raise ConflictError("This pair was already merged", code="duplicate_resolved")
    if dup.status == "open":
        dup.status = "kept_separate"
        dup.resolved_by = principal_of(ctx).id
        dup.resolved_at = now()
        await db.flush()
    sa, sb = await records.summaries(db, [a, b])
    return _dup_out(dup, sa, sb)
