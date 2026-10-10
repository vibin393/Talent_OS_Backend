"""Skill catalog (FR-23, story 3.2): global seed skills + tenant skills, synonyms and the pending queue.

``resolve_skill(text)`` normalises the text and looks it up in the order tenant alias -> tenant slug ->
global alias -> global slug. Unknown text creates a *pending* tenant skill (``create_pending=True``) that a
tenant admin later approves, rejects, or rejects-and-merges into an existing skill.
"""

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.pagination import Page, PageParams, paginate
from app.models.candidates import CandidateSkill, Skill, SkillAlias, TenantSkill, TenantSkillAlias
from app.schemas.candidates import PendingSkillOut, SkillCreate, SkillOut, SkillRef, SkillRefIn
from app.services.candidates.common import normalize_skill, slugify

GLOBAL = "global"
TENANT = "tenant"


def _global_ref(skill: Skill) -> SkillRef:
    return SkillRef(kind=GLOBAL, id=skill.id, name=skill.name, status="approved")


def _tenant_ref(skill: TenantSkill) -> SkillRef:
    return SkillRef(kind=TENANT, id=skill.id, name=skill.name, status=skill.status)


async def _lookup(db: AsyncSession, norm: str) -> SkillRef | None:
    slug = slugify(norm)
    # 1. tenant alias
    alias = (
        await db.execute(select(TenantSkillAlias).where(TenantSkillAlias.alias_norm == norm))
    ).scalar_one_or_none()
    if alias is not None:
        if alias.skill_id is not None:
            skill = await db.get(Skill, alias.skill_id)
            if skill is not None:
                return _global_ref(skill)
        elif alias.tenant_skill_id is not None:
            tskill = await db.get(TenantSkill, alias.tenant_skill_id)
            if tskill is not None:
                return _tenant_ref(tskill)
    # 2. tenant slug
    tskill = (await db.execute(select(TenantSkill).where(TenantSkill.slug == slug))).scalar_one_or_none()
    if tskill is not None:
        return _tenant_ref(tskill)
    # 3. global alias
    galias = (await db.execute(select(SkillAlias).where(SkillAlias.alias_norm == norm))).scalar_one_or_none()
    if galias is not None:
        skill = await db.get(Skill, galias.skill_id)
        if skill is not None and skill.is_active:
            return _global_ref(skill)
    # 4. global slug
    skill = (
        await db.execute(select(Skill).where(Skill.slug == slug, Skill.is_active.is_(True)))
    ).scalar_one_or_none()
    return _global_ref(skill) if skill is not None else None


async def resolve_skill(
    ctx: RequestContext,
    db: AsyncSession,
    text: str,
    create_pending: bool = True,
    created_from: str = "manual",
) -> SkillRef | None:
    """Map free text to a catalog skill; unknown text -> new pending tenant skill (or ``None``)."""
    norm = normalize_skill(text)
    if not norm:
        return None
    found = await _lookup(db, norm)
    if found is not None or not create_pending:
        return found
    display = " ".join(text.split())[:200]
    row = TenantSkill(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        name=display,
        slug=slugify(norm),
        status="pending",
        created_from=created_from,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:  # created concurrently
        return await _lookup(db, norm)
    return _tenant_ref(row)


async def get_ref(db: AsyncSession, ref: SkillRefIn | SkillRef) -> SkillRef | None:
    if ref.kind == GLOBAL:
        skill = await db.get(Skill, ref.id)
        return _global_ref(skill) if skill is not None and skill.is_active else None
    tskill = await db.get(TenantSkill, ref.id)
    return _tenant_ref(tskill) if tskill is not None else None


async def refs_for(
    db: AsyncSession, global_ids: Iterable[uuid.UUID], tenant_ids: Iterable[uuid.UUID]
) -> tuple[dict[uuid.UUID, SkillRef], dict[uuid.UUID, SkillRef]]:
    gids, tids = set(global_ids), set(tenant_ids)
    gmap: dict[uuid.UUID, SkillRef] = {}
    tmap: dict[uuid.UUID, SkillRef] = {}
    if gids:
        for s in (await db.execute(select(Skill).where(Skill.id.in_(gids)))).scalars():
            gmap[s.id] = _global_ref(s)
    if tids:
        for t in (await db.execute(select(TenantSkill).where(TenantSkill.id.in_(tids)))).scalars():
            tmap[t.id] = _tenant_ref(t)
    return gmap, tmap


# ================================================================ catalog API
def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def search_skills(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    q: str | None,
    *,
    include_pending: bool,
    params: PageParams,
) -> Page[SkillOut]:
    """Typeahead over global + tenant skills (names and aliases)."""
    limit = params.page_size
    norm = normalize_skill(q or "")
    gstmt = select(Skill).where(Skill.is_active.is_(True))
    tstmt = select(TenantSkill).where(
        TenantSkill.status.in_(("approved", "pending") if include_pending else ("approved",))
    )
    if norm:
        pattern = _like(norm)
        galias = select(SkillAlias.skill_id).where(SkillAlias.alias_norm.ilike(pattern, escape="\\"))
        gstmt = gstmt.where(or_(Skill.name.ilike(_like(q or ""), escape="\\"), Skill.id.in_(galias)))
        talias = select(TenantSkillAlias.tenant_skill_id).where(
            TenantSkillAlias.alias_norm.ilike(pattern, escape="\\")
        )
        tstmt = tstmt.where(
            or_(TenantSkill.name.ilike(_like(q or ""), escape="\\"), TenantSkill.id.in_(talias))
        )
    # Exact / prefix matches first, then alphabetical.
    exact = func.lower(Skill.name) == (q or "").strip().lower()
    gstmt = gstmt.order_by(exact.desc(), func.length(Skill.name), Skill.name)
    texact = func.lower(TenantSkill.name) == (q or "").strip().lower()
    tstmt = tstmt.order_by(texact.desc(), func.length(TenantSkill.name), TenantSkill.name)

    gcount = int(
        (await db.execute(select(func.count()).select_from(gstmt.order_by(None).subquery()))).scalar_one()
    )
    tcount = int(
        (await db.execute(select(func.count()).select_from(tstmt.order_by(None).subquery()))).scalar_one()
    )
    tenants = list((await db.execute(tstmt.limit(params.offset + limit))).scalars())
    globals_ = list((await db.execute(gstmt.limit(params.offset + limit))).scalars())
    merged: list[SkillOut] = [_tenant_out(t) for t in tenants] + [await _global_out(db, g) for g in globals_]
    items = merged[params.offset : params.offset + limit]
    return Page[SkillOut](items=items, total=gcount + tcount, page=params.page, page_size=params.page_size)


async def _global_out(db: AsyncSession, skill: Skill) -> SkillOut:
    parent = await db.get(Skill, skill.parent_id) if skill.parent_id else None
    return SkillOut(
        kind=GLOBAL,
        id=skill.id,
        name=skill.name,
        status="approved",
        category=skill.category,
        parent_name=parent.name if parent else None,
    )


def _tenant_out(skill: TenantSkill) -> SkillOut:
    return SkillOut(kind=TENANT, id=skill.id, name=skill.name, status=skill.status, category=skill.category)


def _skill_exists() -> ConflictError:
    return ConflictError("A skill with this name already exists", code="skill_exists")


def _alias_exists() -> ConflictError:
    return ConflictError("This synonym is already used by another skill", code="alias_exists")


async def _alias_taken(db: AsyncSession, norm: str) -> bool:
    if (await db.execute(select(TenantSkillAlias.id).where(TenantSkillAlias.alias_norm == norm))).first():
        return True
    return (await db.execute(select(SkillAlias.id).where(SkillAlias.alias_norm == norm))).first() is not None


async def create_skill(ctx: RequestContext, db: AsyncSession, data: SkillCreate) -> SkillOut:
    norm = normalize_skill(data.name)
    if not norm:
        raise ValidationFailed("Skill name is empty")
    existing = await _lookup(db, norm)
    if existing is not None and existing.status != "rejected":
        raise _skill_exists()
    if data.parent_skill_id is not None and await db.get(Skill, data.parent_skill_id) is None:
        raise ValidationFailed(
            "Unknown parent skill", code="invalid_reference", details={"field": "parent_skill_id"}
        )
    row = TenantSkill(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        name=" ".join(data.name.split()),
        slug=slugify(norm),
        category=data.category,
        parent_skill_id=data.parent_skill_id,
        status="approved",
        created_from="admin",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise _skill_exists() from exc
    aliases: list[str] = []
    for alias in data.aliases:
        a = normalize_skill(alias)
        if not a or a == norm or a in aliases:
            continue
        if await _alias_taken(db, a):
            raise _alias_exists()
        db.add(
            TenantSkillAlias(id=uuid.uuid4(), tenant_id=ctx.tenant_id, alias_norm=a, tenant_skill_id=row.id)
        )
        aliases.append(a)
    await db.flush()
    out = _tenant_out(row)
    out.aliases = aliases
    return out


async def add_alias(
    ctx: RequestContext, db: AsyncSession, kind: str, skill_id: uuid.UUID, alias: str
) -> SkillOut:
    if kind not in {GLOBAL, TENANT}:
        raise NotFound("Skill not found")
    norm = normalize_skill(alias)
    if not norm:
        raise ValidationFailed("Synonym is empty")
    if kind == GLOBAL:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise NotFound("Skill not found")
        target: SkillOut = await _global_out(db, skill)
        if slugify(norm) == skill.slug:
            raise _alias_exists()
    else:
        tskill = await db.get(TenantSkill, skill_id)
        if tskill is None:
            raise NotFound("Skill not found")
        target = _tenant_out(tskill)
        if slugify(norm) == tskill.slug:
            raise _alias_exists()
    if await _alias_taken(db, norm):
        raise _alias_exists()
    try:
        async with db.begin_nested():
            db.add(
                TenantSkillAlias(
                    id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    alias_norm=norm,
                    skill_id=skill_id if kind == GLOBAL else None,
                    tenant_skill_id=skill_id if kind == TENANT else None,
                )
            )
            await db.flush()
    except IntegrityError as exc:
        raise _alias_exists() from exc
    target.aliases = [norm]
    return target


async def list_pending(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[PendingSkillOut]:  # noqa: ARG001
    usage = (
        select(func.count())
        .select_from(CandidateSkill)
        .where(CandidateSkill.tenant_skill_id == TenantSkill.id)
        .scalar_subquery()
    )
    stmt = (
        select(TenantSkill, usage.label("usage"))
        .where(TenantSkill.status == "pending")
        .order_by(TenantSkill.created_at.desc(), TenantSkill.id)
    )
    page = await paginate(db, stmt, params)
    items = [
        PendingSkillOut(
            id=t.id,
            name=t.name,
            slug=t.slug,
            category=t.category,
            status=t.status,
            created_from=t.created_from,
            created_at=t.created_at,
            candidate_count=int(n),
        )
        for t, n in page.items
    ]
    return Page[PendingSkillOut](items=items, total=page.total, page=page.page, page_size=page.page_size)


async def _pending_skill(db: AsyncSession, skill_id: uuid.UUID) -> TenantSkill:
    row = await db.get(TenantSkill, skill_id, with_for_update=True)
    if row is None:
        raise NotFound("Skill not found")
    return row


async def approve_skill(ctx: RequestContext, db: AsyncSession, skill_id: uuid.UUID) -> SkillOut:  # noqa: ARG001
    row = await _pending_skill(db, skill_id)
    if row.status == "rejected":
        raise ConflictError("A rejected skill cannot be approved", code="skill_rejected")
    row.status = "approved"
    await db.flush()
    return _tenant_out(row)


async def reject_skill(
    ctx: RequestContext, db: AsyncSession, skill_id: uuid.UUID, merge_into: SkillRefIn | None
) -> SkillOut:
    row = await _pending_skill(db, skill_id)
    if row.status == "approved" and merge_into is None:
        raise ConflictError("An approved skill cannot be rejected", code="skill_approved")
    target: SkillRef | None = None
    if merge_into is not None:
        if merge_into.kind == TENANT and merge_into.id == row.id:
            raise ValidationFailed("A skill cannot be merged into itself", code="invalid_reference")
        target = await get_ref(db, merge_into)
        if target is None or target.status == "rejected":
            raise ValidationFailed(
                "Unknown target skill", code="invalid_reference", details={"field": "merge_into"}
            )
        await _repoint_candidate_skills(db, row.id, target)
        # The rejected name becomes a synonym of the target (re-points any alias of the rejected skill too).
        aliases = (
            await db.execute(select(TenantSkillAlias).where(TenantSkillAlias.tenant_skill_id == row.id))
        ).scalars()
        for alias in aliases:
            alias.tenant_skill_id = target.id if target.kind == TENANT else None
            alias.skill_id = target.id if target.kind == GLOBAL else None
        norm = normalize_skill(row.name)
        if norm and not await _alias_taken(db, norm):
            db.add(
                TenantSkillAlias(
                    id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    alias_norm=norm,
                    skill_id=target.id if target.kind == GLOBAL else None,
                    tenant_skill_id=target.id if target.kind == TENANT else None,
                )
            )
    row.status = "rejected"
    await db.flush()
    return _tenant_out(row)


async def _repoint_candidate_skills(db: AsyncSession, tenant_skill_id: uuid.UUID, target: SkillRef) -> None:
    rows: Sequence[CandidateSkill] = (
        (await db.execute(select(CandidateSkill).where(CandidateSkill.tenant_skill_id == tenant_skill_id)))
        .scalars()
        .all()
    )
    for cs in rows:
        col = CandidateSkill.skill_id if target.kind == GLOBAL else CandidateSkill.tenant_skill_id
        existing = (
            await db.execute(
                select(CandidateSkill).where(CandidateSkill.candidate_id == cs.candidate_id, col == target.id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if cs.years is not None and (existing.years is None or cs.years > existing.years):
                existing.years = cs.years
            if cs.last_used is not None and (existing.last_used is None or cs.last_used > existing.last_used):
                existing.last_used = cs.last_used
            await db.delete(cs)
        else:
            cs.tenant_skill_id = target.id if target.kind == TENANT else None
            cs.skill_id = target.id if target.kind == GLOBAL else None
    await db.flush()
