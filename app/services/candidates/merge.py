"""Merge / undo of duplicate candidates (FR-21, story 3.5).

Merge: snapshot both candidates' scalar fields and the ids of every moved row -> apply field winners ->
move children (dedup), resumes (renumbered), tags, list memberships, attachments, AI actions and activities
to the survivor -> archive the loser with ``merged_into_id``. Undo (within 30 days, once) restores from the
snapshot. Both are audited and emit ``CandidatesMerged`` / ``CandidateMergeUndone``.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, DateTime, Numeric, Uuid, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams
from app.core.permissions import forbidden, scope_condition
from app.events.candidates import CandidateMergeUndone, CandidatesMerged
from app.models.candidates import (
    Candidate,
    CandidateCertification,
    CandidateEducation,
    CandidateEmail,
    CandidateEmployment,
    CandidateLink,
    CandidateListMember,
    CandidateMerge,
    CandidatePhone,
    CandidateResume,
    CandidateSkill,
    CandidateTag,
)
from app.schemas.candidates import MergeIn, MergeOut
from app.services import ai, files_service
from app.services.candidates import activity_port, duplicates, records
from app.services.candidates.common import MERGE, can_access, now, principal_of

UNDO_WINDOW = timedelta(days=30)
MERGEABLE_FIELDS = frozenset(duplicates.COMPARE_FIELDS)
SNAPSHOT_FIELDS = (*records.SCALAR_FIELDS, "archived_at", "merged_into_id")


# ================================================================ snapshot helpers
def _dump(value: Any) -> Any:
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _load(field: str, value: Any) -> Any:
    if value is None:
        return None
    col_type = Candidate.__table__.c[field].type
    if isinstance(col_type, Uuid):
        return uuid.UUID(str(value))
    if isinstance(col_type, Numeric):
        return Decimal(str(value))
    if isinstance(col_type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(col_type, Date):
        return date.fromisoformat(value)
    return value


def _scalars(cand: Candidate) -> dict[str, Any]:
    return {f: _dump(getattr(cand, f)) for f in SNAPSHOT_FIELDS}


def _restore(cand: Candidate, data: dict[str, Any]) -> None:
    for field in SNAPSHOT_FIELDS:
        if field in data:
            setattr(cand, field, _load(field, data[field]))


def _merge_out(row: CandidateMerge) -> MergeOut:
    return MergeOut(
        id=row.id,
        survivor_id=row.survivor_id,
        merged_id=row.merged_id,
        duplicate_id=row.duplicate_id,
        field_choices=dict(row.field_choices or {}),
        merged_by=row.merged_by,
        merged_at=row.merged_at,
        undo_until=row.undo_until,
        undone_at=row.undone_at,
        can_undo=row.undone_at is None and now() < row.undo_until,
    )


async def _require_merge(ctx: RequestContext, db: AsyncSession, *ids: uuid.UUID) -> None:
    principal = principal_of(ctx)
    for cid in ids:
        if not await can_access(db, principal, cid, MERGE):
            raise forbidden(MERGE)


async def _rows(db: AsyncSession, model: Any, candidate_id: uuid.UUID) -> list[Any]:
    return list((await db.execute(select(model).where(model.candidate_id == candidate_id))).scalars())


# ================================================================ merge
async def merge(ctx: RequestContext, db: AsyncSession, duplicate_id: uuid.UUID, body: MergeIn) -> MergeOut:
    dup, a, b = await duplicates.get_pair(ctx, db, duplicate_id, for_update=True)
    if dup.status != "open":
        raise ConflictError("This duplicate pair is already resolved", code="duplicate_resolved")
    if body.survivor_id not in {a.id, b.id}:
        raise ValidationFailed(
            "survivor_id must be one of the pair",
            details={"fields": [{"field": "survivor_id", "message": "not part of this pair"}]},
        )
    unknown = sorted(set(body.field_choices) - MERGEABLE_FIELDS)
    if unknown:
        raise ValidationFailed(
            "Unknown merge fields",
            details={"fields": [{"field": f"field_choices.{f}", "message": "unknown"} for f in unknown]},
        )
    await _require_merge(ctx, db, a.id, b.id)
    # Lock both candidates in a stable order.
    for cid in sorted((a.id, b.id)):
        await db.execute(select(Candidate.id).where(Candidate.id == cid).with_for_update())
    survivor, loser = (a, b) if body.survivor_id == a.id else (b, a)
    if survivor.merged_into_id is not None or loser.merged_into_id is not None:
        raise ConflictError("One of the candidates was already merged", code="duplicate_resolved")
    tid = ctx.tenant_id
    snapshot: dict[str, Any] = {
        "survivor": _scalars(survivor),
        "merged": _scalars(loser),
        "moved": {},
        "resume_versions": {},
        "skill_values": {},
        "primary": [],
    }

    # 2. field winners
    by_side = {"a": a, "b": b}
    for field, side in body.field_choices.items():
        winner = by_side[side]
        if winner.id == loser.id:
            setattr(survivor, field, getattr(loser, field))
    await db.flush()

    moved: dict[str, list[str]] = snapshot["moved"]

    # 3a. contact rows (skip values the survivor already has)
    contact_specs: tuple[tuple[str, Any, str], ...] = (
        ("emails", CandidateEmail, "email"),
        ("phones", CandidatePhone, "phone_e164"),
        ("links", CandidateLink, "url_norm"),
    )
    for key, model, attr in contact_specs:
        have = {getattr(r, attr) for r in await _rows(db, model, survivor.id)}
        moved[key] = []
        for row in await _rows(db, model, loser.id):
            if getattr(row, attr) in have:
                continue
            if getattr(row, "is_primary", False):
                snapshot["primary"].append(str(row.id))
                row.is_primary = False
            row.candidate_id = survivor.id
            moved[key].append(str(row.id))
            have.add(getattr(row, attr))
    # 3b. history rows
    history_specs: tuple[tuple[str, Any], ...] = (
        ("employment", CandidateEmployment),
        ("education", CandidateEducation),
        ("certifications", CandidateCertification),
    )
    for key, model in history_specs:
        moved[key] = []
        for row in await _rows(db, model, loser.id):
            row.candidate_id = survivor.id
            moved[key].append(str(row.id))
    # 3c. skills (dedup keeping max years / latest last_used)
    moved["skills"] = []
    mine: dict[tuple[str, Any], CandidateSkill] = {
        (("g", r.skill_id) if r.skill_id else ("t", r.tenant_skill_id)): r
        for r in await _rows(db, CandidateSkill, survivor.id)
    }
    for row in await _rows(db, CandidateSkill, loser.id):
        skey = ("g", row.skill_id) if row.skill_id else ("t", row.tenant_skill_id)
        existing = mine.get(skey)
        if existing is None:
            row.candidate_id = survivor.id
            moved["skills"].append(str(row.id))
            continue
        snapshot["skill_values"][str(existing.id)] = {
            "years": _dump(existing.years),
            "last_used": _dump(existing.last_used),
        }
        if row.years is not None and (existing.years is None or row.years > existing.years):
            existing.years = row.years
        if row.last_used is not None and (existing.last_used is None or row.last_used > existing.last_used):
            existing.last_used = row.last_used
    # 3d. resumes (renumbered after the survivor's versions)
    await db.flush()
    top = int(
        (
            await db.execute(
                select(func.coalesce(func.max(CandidateResume.version), 0)).where(
                    CandidateResume.candidate_id == survivor.id
                )
            )
        ).scalar_one()
        or 0
    )
    moved["resumes"] = []
    loser_resumes = sorted(await _rows(db, CandidateResume, loser.id), key=lambda r: r.version or 0)
    for i, row in enumerate(loser_resumes, start=1):
        snapshot["resume_versions"][str(row.id)] = row.version
        row.candidate_id = survivor.id
        row.version = top + i
        moved["resumes"].append(str(row.id))
        await db.flush()
    # 3e. tags and list memberships
    for key, model, attr in (
        ("tags", CandidateTag, "tag_id"),
        ("list_members", CandidateListMember, "list_id"),
    ):
        have = {getattr(r, attr) for r in await _rows(db, model, survivor.id)}
        moved[key] = []
        for row in await _rows(db, model, loser.id):
            if getattr(row, attr) in have:
                continue
            row.candidate_id = survivor.id
            moved[key].append(str(row.id))
    await db.flush()
    # 3f. attachments, AI actions, activities (other modules, through their services)
    moved["attachments"] = [
        str(i) for i in await files_service.reassign_attachments(db, "candidate", loser.id, survivor.id)
    ]
    moved["ai_actions"] = [str(i) for i in await ai.reassign_subject(db, "candidate", loser.id, survivor.id)]
    moved["activities"] = [
        str(i) for i in await activity_port.reassign_entity(ctx, db, "candidate", loser.id, survivor.id)
    ]

    # 4. archive the loser
    at = now()
    loser.merged_into_id = survivor.id
    loser.archived_at = at
    dup.status = "merged"
    dup.resolved_by = principal_of(ctx).id
    dup.resolved_at = at
    row = CandidateMerge(
        id=uuid.uuid4(),
        tenant_id=tid,
        survivor_id=survivor.id,
        merged_id=loser.id,
        duplicate_id=dup.id,
        field_choices=dict(body.field_choices),
        snapshot=snapshot,
        merged_by=principal_of(ctx).id,
        merged_at=at,
        undo_until=at + UNDO_WINDOW,
    )
    db.add(row)
    await db.flush()

    # 5. audit + event + timeline
    audit.record(
        "candidate.merged",
        "candidate",
        survivor.id,
        session=db,
        merge_id=row.id,
        merged_id=loser.id,
        duplicate_id=dup.id,
        fields=sorted(body.field_choices),
    )
    emit(db, CandidatesMerged(tenant_id=tid, merge_id=row.id, survivor_id=survivor.id, merged_id=loser.id))  # type: ignore[arg-type]
    await activity_port.add_system_entry(
        ctx, db, "candidate", survivor.id, "Candidate merged", merge_id=str(row.id), merged_id=str(loser.id)
    )
    return _merge_out(row)


# ================================================================ undo
async def undo(ctx: RequestContext, db: AsyncSession, merge_id: uuid.UUID) -> MergeOut:
    row = await _get_merge(ctx, db, merge_id, for_update=True)
    if row.undone_at is not None:
        raise ConflictError("This merge was already undone", code="merge_already_undone")
    if now() >= row.undo_until:
        raise ConflictError("The 30-day undo window has passed", code="merge_undo_expired")
    await _require_merge(ctx, db, row.survivor_id, row.merged_id)
    survivor = await db.get(Candidate, row.survivor_id, with_for_update=True)
    loser = await db.get(Candidate, row.merged_id, with_for_update=True)
    if survivor is None or loser is None:  # pragma: no cover - candidates are never hard-deleted
        raise NotFound("Candidate not found")
    snap: dict[str, Any] = row.snapshot or {}
    moved: dict[str, list[str]] = snap.get("moved", {})
    primary = set(snap.get("primary", []))

    undo_specs: tuple[tuple[str, Any], ...] = (
        ("emails", CandidateEmail),
        ("phones", CandidatePhone),
        ("links", CandidateLink),
        ("employment", CandidateEmployment),
        ("education", CandidateEducation),
        ("certifications", CandidateCertification),
        ("skills", CandidateSkill),
        ("tags", CandidateTag),
        ("list_members", CandidateListMember),
    )
    for key, model in undo_specs:
        for rid in moved.get(key, []):
            child: Any = await db.get(model, uuid.UUID(rid))
            if child is None or child.candidate_id != survivor.id:
                continue
            child.candidate_id = loser.id
            if rid in primary:
                child.is_primary = True
    # Resumes back with their original version numbers (move first, then renumber).
    versions: dict[str, int | None] = snap.get("resume_versions", {})
    for rid in moved.get("resumes", []):
        resume = await db.get(CandidateResume, uuid.UUID(rid))
        if resume is not None and resume.candidate_id == survivor.id:
            resume.candidate_id = loser.id
            resume.version = (
                -abs(resume.version or 0) or None
            )  # temporary, avoids (candidate, version) clashes
    await db.flush()
    for rid in moved.get("resumes", []):
        resume = await db.get(CandidateResume, uuid.UUID(rid))
        if resume is not None and resume.candidate_id == loser.id:
            resume.version = versions.get(rid)
    for sid, values in snap.get("skill_values", {}).items():
        skill_row = await db.get(CandidateSkill, uuid.UUID(sid))
        if skill_row is not None:
            skill_row.years = Decimal(values["years"]) if values.get("years") is not None else None
            skill_row.last_used = date.fromisoformat(values["last_used"]) if values.get("last_used") else None
    await files_service.reassign_attachments(
        db, "candidate", survivor.id, loser.id, only_ids=[uuid.UUID(i) for i in moved.get("attachments", [])]
    )
    await ai.reassign_subject(
        db, "candidate", survivor.id, loser.id, only_ids=[uuid.UUID(i) for i in moved.get("ai_actions", [])]
    )
    await activity_port.reassign_activities(
        ctx, db, "candidate", [uuid.UUID(i) for i in moved.get("activities", [])], loser.id
    )
    _restore(survivor, snap.get("survivor", {}))
    _restore(loser, snap.get("merged", {}))
    at = now()
    row.undone_at = at
    if row.duplicate_id is not None:
        from app.models.candidates import CandidateDuplicate

        dup = await db.get(CandidateDuplicate, row.duplicate_id)
        if dup is not None:
            dup.status = "open"
            dup.resolved_by = None
            dup.resolved_at = None
    await db.flush()
    audit.record(
        "candidate.merge_undone", "candidate", survivor.id, session=db, merge_id=row.id, merged_id=loser.id
    )
    emit(
        db,
        CandidateMergeUndone(
            tenant_id=ctx.tenant_id,  # type: ignore[arg-type]
            merge_id=row.id,
            survivor_id=survivor.id,
            merged_id=loser.id,
        ),
    )
    await activity_port.add_system_entry(
        ctx, db, "candidate", survivor.id, "Merge undone", merge_id=str(row.id)
    )
    return _merge_out(row)


# ================================================================ listing
def _visible_merges(ctx: RequestContext) -> Any:
    principal = principal_of(ctx)
    s = aliased(Candidate)
    m = aliased(Candidate)
    stmt = (
        select(CandidateMerge)
        .join(s, s.id == CandidateMerge.survivor_id)
        .join(m, m.id == CandidateMerge.merged_id)
    )
    for alias in (s, m):
        cond = scope_condition(principal, "candidate:read", alias.owner_id)
        if cond is not None:
            stmt = stmt.where(cond)
    return stmt


async def _get_merge(
    ctx: RequestContext, db: AsyncSession, merge_id: uuid.UUID, *, for_update: bool = False
) -> CandidateMerge:
    stmt = _visible_merges(ctx).where(CandidateMerge.id == merge_id)
    if for_update:
        stmt = stmt.with_for_update(of=CandidateMerge)
    row: CandidateMerge | None = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Merge not found")
    return row


async def list_merges(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[MergeOut]:
    stmt = _visible_merges(ctx)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(CandidateMerge.merged_at.desc(), CandidateMerge.id)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).scalars()
    return Page[MergeOut](
        items=[_merge_out(r) for r in rows], total=total, page=params.page, page_size=params.page_size
    )
