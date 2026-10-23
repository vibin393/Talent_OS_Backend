# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Employees (Epic 41 A1, AD-50 -- no new principal).

* Worker = a ``candidate`` principal with a non-terminal Placement; its Candidate is the applicant.
* User = an active internal ``users`` row; on the first internal application ``candidate_for_user`` links or
  creates a Candidate (P1 candidates service, duplicate detection by email, source ``internal``) and stores
  ``mobility_employee_links``.
* ``mobility_profiles`` -- optional grade / location used only by eligibility (``mobility:manage``; CSV import).

Every function here runs in an internal tenant session (the Worker portal router resolves the Worker first).
"""

import csv
import dataclasses
import io
import uuid
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.models.mobility import MobilityEmployeeLink, MobilityProfile
from app.schemas.mobility import ProfileImportOut, ProfileImportRow, ProfileIn, ProfileOut, ProfilePreviewOut
from app.services.mobility.common import (
    MANAGE,
    OPEN_PLACEMENT_STATUSES,
    invalid,
    not_found,
    now,
    require,
    tenant_of,
)

EmployeeKind = Literal["user", "worker"]


@dataclasses.dataclass(frozen=True, slots=True)
class Employee:
    kind: EmployeeKind
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    candidate_id: uuid.UUID | None  # Worker: always; User: the linked Candidate (None before the first application)
    placement_id: uuid.UUID | None  # Worker: the current Placement
    tenure_start: date | None
    grade: str | None
    location: str | None
    business_unit_id: uuid.UUID | None
    name: str
    email: str | None
    account_manager_user_id: uuid.UUID | None = None

    @property
    def subject_id(self) -> uuid.UUID:
        sid = self.user_id if self.kind == "user" else self.candidate_id
        assert sid is not None
        return sid


async def _profile(db: AsyncSession, kind: str, subject_id: uuid.UUID) -> MobilityProfile | None:
    return (
        await db.execute(
            select(MobilityProfile).where(MobilityProfile.subject_kind == kind, MobilityProfile.subject_id == subject_id)
        )
    ).scalar_one_or_none()


async def linked_candidate(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(select(MobilityEmployeeLink.candidate_id).where(MobilityEmployeeLink.user_id == user_id))
    ).scalar_one_or_none()


async def user_employee(db: AsyncSession, user_id: uuid.UUID) -> Employee:
    from app.models.identity import User

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Forbidden("Internal mobility is open to active users", code="forbidden")
    profile = await _profile(db, "user", user.id)
    return Employee(
        kind="user",
        tenant_id=user.tenant_id,
        user_id=user.id,
        candidate_id=await linked_candidate(db, user.id),
        placement_id=None,
        tenure_start=user.created_at.date() if user.created_at else None,
        grade=profile.grade if profile else None,
        location=profile.location if profile else None,
        business_unit_id=user.business_unit_id,
        name=user.name,
        email=str(user.email),
    )


async def current_placement(db: AsyncSession, candidate_id: uuid.UUID) -> object | None:
    """The Worker's current non-terminal Placement (active / offboarding first, then the latest start)."""
    from app.models.placements import Placement

    rows = list(
        (
            await db.execute(
                select(Placement).where(
                    Placement.candidate_id == candidate_id, Placement.status.in_(OPEN_PLACEMENT_STATUSES)
                )
            )
        ).scalars()
    )
    if not rows:
        return None
    rank = {"active": 0, "offboarding": 1, "joining_confirmed": 2, "pre_joining": 3}
    rows.sort(key=lambda p: (rank.get(p.status, 9), -(p.actual_start_date or p.planned_start_date).toordinal()))
    return rows[0]


async def worker_employee(db: AsyncSession, candidate_id: uuid.UUID) -> Employee:
    """A Worker: a live Candidate with a non-terminal Placement (else 403 ``not_a_worker``)."""
    from app.models.candidates import Candidate
    from app.services.candidates import portal_port

    cand = await db.get(Candidate, candidate_id)
    placement = await current_placement(db, candidate_id)
    if cand is None or cand.merged_into_id is not None or cand.erased_at is not None or placement is None:
        raise Forbidden("Internal openings are open to workers on an assignment", code="not_a_worker")
    profile = await _profile(db, "worker", candidate_id)
    return Employee(
        kind="worker",
        tenant_id=cand.tenant_id,
        user_id=None,
        candidate_id=candidate_id,
        placement_id=placement.id,  # type: ignore[attr-defined]
        tenure_start=placement.actual_start_date,  # type: ignore[attr-defined]
        grade=profile.grade if profile else None,
        location=placement.work_location_text or (profile.location if profile else None),  # type: ignore[attr-defined]
        business_unit_id=None,
        name=cand.full_name,
        email=await portal_port.primary_email(db, candidate_id),
        account_manager_user_id=placement.account_manager_user_id,  # type: ignore[attr-defined]
    )


async def _find_by_email(ctx: RequestContext, db: AsyncSession, email: str) -> uuid.UUID | None:
    from app.models.candidates import Candidate
    from app.services.candidates import duplicates

    cid = await duplicates.link_by_exact_email(ctx, db, email)
    if cid is None:
        return None
    cand = await db.get(Candidate, cid)
    if cand is None or cand.erased_at is not None or cand.merged_into_id is not None:
        return None
    taken = (
        await db.execute(select(MobilityEmployeeLink.user_id).where(MobilityEmployeeLink.candidate_id == cid))
    ).scalar_one_or_none()
    return None if taken is not None else cid


async def profile_preview(ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID) -> ProfilePreviewOut:
    """What the User applies with: the linked Candidate, an existing one found by email, or a new one."""
    from app.models.candidates import Candidate
    from app.models.search import CandidateSearchDoc

    emp = await user_employee(db, user_id)
    cid = emp.candidate_id or (await _find_by_email(ctx, db, emp.email) if emp.email else None)
    if cid is None:
        return ProfilePreviewOut(
            candidate_id=None, will_create=True, full_name=emp.name, email=emp.email or "", current_title=None, skills=[]
        )
    cand = await db.get(Candidate, cid)
    doc = await db.get(CandidateSearchDoc, cid)
    return ProfilePreviewOut(
        candidate_id=cid,
        will_create=False,
        full_name=cand.full_name if cand else emp.name,
        email=emp.email or "",
        current_title=cand.current_title if cand else None,
        skills=list(doc.skill_names or [])[:30] if doc else [],
    )


async def candidate_for_user(ctx: RequestContext, db: AsyncSession, emp: Employee) -> uuid.UUID:
    """Link (or create) the User's Candidate and store ``mobility_employee_links`` (idempotent)."""
    from app.schemas.candidates import CandidateCreate, EmailIn
    from app.services.candidates import duplicates, records

    assert emp.kind == "user" and emp.user_id is not None
    if emp.candidate_id is not None:
        return emp.candidate_id
    cid = await _find_by_email(ctx, db, emp.email) if emp.email else None
    if cid is None:
        data = CandidateCreate(
            full_name=emp.name,
            source_detail="Internal employee",
            emails=[EmailIn(email=emp.email, is_primary=True)] if emp.email else [],
        )
        cand, _ = await records.create_record(ctx, db, data, owner_id=emp.user_id, source="internal")
        await duplicates.find_duplicates(ctx, db, cand)
        cid = cand.id
    db.add(MobilityEmployeeLink(id=uuid.uuid4(), tenant_id=emp.tenant_id, user_id=emp.user_id, candidate_id=cid, linked_at=now()))
    await db.flush()
    return cid


async def linked_user_candidates(db: AsyncSession) -> dict[uuid.UUID, uuid.UUID]:
    """candidate_id -> user_id of every linked internal User."""
    rows = await db.execute(select(MobilityEmployeeLink.candidate_id, MobilityEmployeeLink.user_id))
    return dict(rows.tuples().all())


# ================================================================ profiles (mobility:manage)
async def _subject_names(
    db: AsyncSession, rows: list[MobilityProfile]
) -> dict[tuple[str, uuid.UUID], tuple[str | None, str | None]]:
    from app.models.candidates import Candidate
    from app.models.identity import User
    from app.models.search import CandidateSearchDoc

    out: dict[tuple[str, uuid.UUID], tuple[str | None, str | None]] = {}
    users = [r.subject_id for r in rows if r.subject_kind == "user"]
    cands = [r.subject_id for r in rows if r.subject_kind == "worker"]
    if users:
        for uid, name, email in (await db.execute(select(User.id, User.name, User.email).where(User.id.in_(users)))).tuples():
            out["user", uid] = (name, str(email))
    if cands:
        for cid, name, email in (
            await db.execute(
                select(Candidate.id, Candidate.full_name, CandidateSearchDoc.email)
                .outerjoin(CandidateSearchDoc, CandidateSearchDoc.candidate_id == Candidate.id)
                .where(Candidate.id.in_(cands))
            )
        ).tuples():
            out["worker", cid] = (name, str(email) if email else None)
    return out


async def list_profiles(ctx: RequestContext, db: AsyncSession, subject_kind: str | None) -> list[ProfileOut]:
    require(ctx, MANAGE)
    stmt = select(MobilityProfile).order_by(MobilityProfile.subject_kind, MobilityProfile.updated_at.desc())
    if subject_kind:
        stmt = stmt.where(MobilityProfile.subject_kind == subject_kind)
    rows = list((await db.execute(stmt)).scalars())
    names = await _subject_names(db, rows)
    return [
        ProfileOut(
            id=r.id,
            subject_kind=r.subject_kind,
            subject_id=r.subject_id,
            subject_name=names.get((r.subject_kind, r.subject_id), (None, None))[0],
            subject_email=names.get((r.subject_kind, r.subject_id), (None, None))[1],
            grade=r.grade,
            location=r.location,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


async def _subject_exists(db: AsyncSession, kind: str, subject_id: uuid.UUID) -> bool:
    from app.models.candidates import Candidate
    from app.models.identity import User

    model = User if kind == "user" else Candidate
    return await db.get(model, subject_id) is not None


async def _upsert(ctx: RequestContext, db: AsyncSession, kind: str, subject_id: uuid.UUID, grade: str | None, location: str | None) -> MobilityProfile:
    row = await _profile(db, kind, subject_id)
    actor = getattr(ctx.principal, "id", None)
    if row is None:
        row = MobilityProfile(id=uuid.uuid4(), tenant_id=tenant_of(db), subject_kind=kind, subject_id=subject_id)
        db.add(row)
    row.grade = grade or None
    row.location = location or None
    row.updated_by = actor if getattr(ctx.principal, "type", None) == "user" else None
    row.updated_at = now()
    await db.flush()
    return row


async def upsert_profile(ctx: RequestContext, db: AsyncSession, data: ProfileIn) -> ProfileOut:
    require(ctx, MANAGE)
    if not await _subject_exists(db, data.subject_kind, data.subject_id):
        raise not_found("Employee")
    row = await _upsert(ctx, db, data.subject_kind, data.subject_id, data.grade, data.location)
    name, email = (await _subject_names(db, [row])).get((row.subject_kind, row.subject_id), (None, None))
    return ProfileOut(
        id=row.id,
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        subject_name=name,
        subject_email=email,
        grade=row.grade,
        location=row.location,
        updated_at=row.updated_at,
    )


async def _resolve_email(db: AsyncSession, email: str) -> tuple[str | None, uuid.UUID | None]:
    """An internal User first, else a Worker (a Candidate with a non-terminal Placement) with that email."""
    from app.models.candidates import CandidateEmail
    from app.models.identity import User
    from app.models.placements import Placement
    from app.services.candidates.common import normalize_email

    uid = (await db.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
    if uid is not None:
        return "user", uid
    norm = normalize_email(email)
    if norm is None:
        return None, None
    cid = (
        await db.execute(
            select(CandidateEmail.candidate_id)
            .join(Placement, Placement.candidate_id == CandidateEmail.candidate_id)
            .where(CandidateEmail.email == norm, Placement.status.in_(OPEN_PLACEMENT_STATUSES))
            .limit(1)
        )
    ).scalar_one_or_none()
    return ("worker", cid) if cid is not None else (None, None)


MAX_IMPORT_ROWS = 5000


async def import_profiles(ctx: RequestContext, db: AsyncSession, content: bytes, *, validate_only: bool) -> ProfileImportOut:
    """CSV (``email,grade,location``) -> preview rows; applied when ``validate_only`` is false and no row errs."""
    require(ctx, MANAGE)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise invalid("invalid_csv", "The file must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    if "email" not in fields:
        raise invalid("invalid_csv", "The CSV needs an 'email' column (and 'grade', 'location')")
    rows: list[ProfileImportRow] = []
    for i, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_IMPORT_ROWS:
            raise invalid("too_many_rows", f"At most {MAX_IMPORT_ROWS} rows per import")
        rec = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        email = rec.get("email", "")
        grade = rec.get("grade") or None
        location = rec.get("location") or None
        error = None
        kind, sid = (None, None)
        if not email:
            error = "email is required"
        elif grade is not None and len(grade) > 40:
            error = "grade is longer than 40 characters"
        elif location is not None and len(location) > 200:
            error = "location is longer than 200 characters"
        else:
            kind, sid = await _resolve_email(db, email)
            if sid is None:
                error = "no internal user or worker on assignment has this email"
        rows.append(
            ProfileImportRow(line=i, email=email, subject_kind=kind, subject_id=sid, grade=grade, location=location, error=error)
        )
    errors = sum(1 for r in rows if r.error)
    applied = 0
    if not validate_only and errors == 0:
        for r in rows:
            assert r.subject_kind is not None and r.subject_id is not None
            await _upsert(ctx, db, r.subject_kind, r.subject_id, r.grade, r.location)
            applied += 1
    return ProfileImportOut(validate_only=validate_only, rows=rows, applied=applied, errors=errors)
