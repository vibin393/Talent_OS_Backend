# ruff: file-ignore[line-too-long]
"""Institutions and placement-officer contacts (Epic 43 story 43.1, FR-217; coordinator decision 13).

Placement officers follow the P1 Contact rules: no login, citext email unique per Institution, E.164 phone
(normalised; 422 ``invalid_phone``), name/email/phone are PII (audit-encrypted, erased by ``campus.records``).
Archiving an Institution with an open drive is refused (409 ``institution_in_use``). ``import_institution`` is the
FR-263 go-live import.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.campus import CampusDrive, DriveInstitution, Institution, InstitutionContact
from app.schemas.campus import (
    ContactIn,
    ContactOut,
    ContactPatch,
    InstitutionDetailOut,
    InstitutionImportIn,
    InstitutionIn,
    InstitutionOut,
    InstitutionPatch,
    Program,
)
from app.services.campus.common import conflict, invalid, now, tenant_of

OPEN_DRIVE_STATUSES = ("draft", "registration_open", "in_progress")


def _phone(raw: str | None) -> str | None:
    from app.services.candidates.common import normalize_phone

    if raw is None or not raw.strip():
        return None
    out = normalize_phone(raw)
    if out is None:
        raise invalid("invalid_phone", "Enter a valid phone number", {"phone": "Enter a valid phone number"})
    return out


def _programs(programs: list[Program]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in programs:
        key = p.name.strip().lower()
        if key in seen:
            raise invalid("duplicate_program", f"Program {p.name} is listed twice", {"programs": "Duplicate program"})
        seen.add(key)
        out.append({"name": p.name.strip(), "branches": list(p.branches)})
    return out


async def _get(db: AsyncSession, institution_id: uuid.UUID, *, lock: bool = False) -> Institution:
    stmt = select(Institution).where(Institution.id == institution_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Institution not found")
    return row


async def _contacts(db: AsyncSession, institution_id: uuid.UUID, *, include_archived: bool) -> list[InstitutionContact]:
    stmt = select(InstitutionContact).where(InstitutionContact.institution_id == institution_id)
    if not include_archived:
        stmt = stmt.where(InstitutionContact.archived_at.is_(None))
    return list((await db.execute(stmt.order_by(InstitutionContact.is_primary.desc(), InstitutionContact.name))).scalars())


async def _out(db: AsyncSession, row: Institution) -> InstitutionDetailOut:
    contacts = await _contacts(db, row.id, include_archived=False)
    primary = next((c for c in contacts if c.is_primary), None)
    return InstitutionDetailOut(
        id=row.id,
        name=str(row.name),
        city=row.city,
        state=row.state,
        country=row.country,
        tier=row.tier,  # type: ignore[arg-type]
        programs=list(row.programs or []),
        website=row.website,
        status=row.status,  # type: ignore[arg-type]
        notes=row.notes,
        contact_count=len(contacts),
        primary_contact_name=primary.name if primary else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        contacts=[ContactOut.model_validate(c) for c in contacts],
    )


async def list_institutions(
    db: AsyncSession, *, q: str | None = None, status: str | None = None, tier: str | None = None
) -> list[InstitutionOut]:
    stmt = select(Institution)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(Institution.name.ilike(like, escape="\\") | Institution.city.ilike(like, escape="\\"))
    if status:
        stmt = stmt.where(Institution.status == status)
    if tier:
        stmt = stmt.where(Institution.tier == tier)
    rows = list((await db.execute(stmt.order_by(Institution.name).limit(1000))).scalars())
    counts = dict(
        (
            await db.execute(
                select(InstitutionContact.institution_id, func.count())
                .where(InstitutionContact.archived_at.is_(None), InstitutionContact.institution_id.in_([r.id for r in rows]))
                .group_by(InstitutionContact.institution_id)
            )
        ).tuples().all()
    ) if rows else {}
    primaries = dict(
        (
            await db.execute(
                select(InstitutionContact.institution_id, InstitutionContact.name).where(
                    InstitutionContact.archived_at.is_(None),
                    InstitutionContact.is_primary.is_(True),
                    InstitutionContact.institution_id.in_([r.id for r in rows]),
                )
            )
        ).tuples().all()
    ) if rows else {}
    return [
        InstitutionOut(
            id=r.id, name=str(r.name), city=r.city, state=r.state, country=r.country, tier=r.tier,  # type: ignore[arg-type]
            programs=list(r.programs or []), website=r.website, status=r.status, notes=r.notes,  # type: ignore[arg-type]
            contact_count=int(counts.get(r.id, 0)), primary_contact_name=primaries.get(r.id),
            created_at=r.created_at, updated_at=r.updated_at,
        )
        for r in rows
    ]


async def _flush_unique(db: AsyncSession, name: str) -> None:
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise conflict("institution_exists", f"An institution named {name} already exists") from exc


async def create_institution(db: AsyncSession, data: InstitutionIn) -> InstitutionDetailOut:
    row = Institution(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name.strip(),
        city=data.city,
        state=data.state,
        country=data.country,
        tier=data.tier,
        programs=_programs(data.programs),
        website=data.website,
        notes=data.notes,
        status="active",
    )
    db.add(row)
    await _flush_unique(db, data.name)
    await db.refresh(row)
    return await _out(db, row)


async def get_institution(db: AsyncSession, institution_id: uuid.UUID) -> InstitutionDetailOut:
    return await _out(db, await _get(db, institution_id))


async def update_institution(db: AsyncSession, institution_id: uuid.UUID, data: InstitutionPatch) -> InstitutionDetailOut:
    row = await _get(db, institution_id, lock=True)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("status") == "archived" and row.status != "archived":
        await _assert_not_in_use(db, row.id)
    for key, value in changes.items():
        if key == "programs":
            row.programs = _programs(data.programs or [])
        elif key == "name" and value is not None:
            row.name = value.strip()
        elif key == "country" and value is not None:
            row.country = value.upper()
        elif value is not None or key in ("city", "state", "website", "notes"):
            setattr(row, key, value)
    await _flush_unique(db, str(row.name))
    await db.refresh(row)
    return await _out(db, row)


async def _assert_not_in_use(db: AsyncSession, institution_id: uuid.UUID) -> None:
    used = (
        await db.execute(
            select(DriveInstitution.id)
            .join(CampusDrive, CampusDrive.id == DriveInstitution.drive_id)
            .where(DriveInstitution.institution_id == institution_id, CampusDrive.status.in_(OPEN_DRIVE_STATUSES))
            .limit(1)
        )
    ).first()
    if used is not None:
        raise conflict("institution_in_use", "This institution is part of an open campus drive")


async def archive_institution(db: AsyncSession, institution_id: uuid.UUID) -> InstitutionDetailOut:
    row = await _get(db, institution_id, lock=True)
    if row.status != "archived":
        await _assert_not_in_use(db, row.id)
        row.status = "archived"
        await db.flush()
    await db.refresh(row)
    return await _out(db, row)


# ================================================================ placement officers
async def list_contacts(db: AsyncSession, institution_id: uuid.UUID, *, include_archived: bool = False) -> list[ContactOut]:
    await _get(db, institution_id)
    return [ContactOut.model_validate(c) for c in await _contacts(db, institution_id, include_archived=include_archived)]


async def _clear_primary(db: AsyncSession, institution_id: uuid.UUID, keep: uuid.UUID) -> None:
    for c in await _contacts(db, institution_id, include_archived=False):
        if c.id != keep and c.is_primary:
            c.is_primary = False


async def _flush_contact(db: AsyncSession) -> None:
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise conflict("contact_exists", "A placement officer with this email already exists for the institution") from exc


async def add_contact(db: AsyncSession, institution_id: uuid.UUID, data: ContactIn) -> ContactOut:
    inst = await _get(db, institution_id)
    row = InstitutionContact(
        id=uuid.uuid4(),
        tenant_id=inst.tenant_id,
        institution_id=inst.id,
        name=data.name.strip(),
        title=data.title,
        email=str(data.email).lower() if data.email else None,
        phone=_phone(data.phone),
        is_primary=data.is_primary,
    )
    db.add(row)
    await _flush_contact(db)
    if row.is_primary:
        await _clear_primary(db, inst.id, row.id)
        await db.flush()
    await db.refresh(row)
    return ContactOut.model_validate(row)


async def _contact(db: AsyncSession, institution_id: uuid.UUID, contact_id: uuid.UUID) -> InstitutionContact:
    row = await db.get(InstitutionContact, contact_id, with_for_update=True)
    if row is None or row.institution_id != institution_id:
        raise NotFound("Placement officer not found")
    return row


async def update_contact(db: AsyncSession, institution_id: uuid.UUID, contact_id: uuid.UUID, data: ContactPatch) -> ContactOut:
    row = await _contact(db, institution_id, contact_id)
    if row.archived_at is not None:
        raise conflict("contact_archived", "This placement officer is archived")
    changes = data.model_dump(exclude_unset=True)
    if changes.get("name"):
        row.name = changes["name"].strip()
    if "title" in changes:
        row.title = changes["title"]
    if "email" in changes:
        row.email = str(changes["email"]).lower() if changes["email"] else None
    if "phone" in changes:
        row.phone = _phone(changes["phone"])
    if changes.get("is_primary") is not None:
        row.is_primary = bool(changes["is_primary"])
    await _flush_contact(db)
    if row.is_primary:
        await _clear_primary(db, institution_id, row.id)
        await db.flush()
    await db.refresh(row)
    return ContactOut.model_validate(row)


async def archive_contact(db: AsyncSession, institution_id: uuid.UUID, contact_id: uuid.UUID) -> ContactOut:
    row = await _contact(db, institution_id, contact_id)
    if row.archived_at is None:
        row.archived_at = now()
        row.is_primary = False
        await db.flush()
    await db.refresh(row)
    return ContactOut.model_validate(row)


# ================================================================ FR-263 go-live import
def _parse_programs(raw: str | None) -> list[Program]:
    """``"B.Tech:CS|IT;MBA"`` -> programs with branches."""
    out: list[Program] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, branches = part.partition(":")
        out.append(Program(name=name.strip(), branches=[b.strip() for b in branches.split("|") if b.strip()]))
    return out


async def import_institution(db: AsyncSession, data: InstitutionImportIn, *, validate_only: bool = False) -> uuid.UUID | None:
    """Create one Institution (and an optional primary placement officer) from a go-live CSV row. Existing names
    -> 409 ``institution_exists``. ``validate_only`` checks everything and writes nothing."""
    programs = _programs(_parse_programs(data.programs))
    phone = _phone(data.contact_phone)
    exists = (await db.execute(select(Institution.id).where(Institution.name == data.name.strip()))).first()
    if exists is not None:
        raise conflict("institution_exists", f"An institution named {data.name} already exists")
    if data.contact_name is None and (data.contact_email or data.contact_phone):
        raise invalid("contact_name_required", "contact_name is required with contact details", {"contact_name": "Required"})
    if validate_only:
        return None
    row = Institution(
        id=uuid.uuid4(), tenant_id=tenant_of(db), name=data.name.strip(), city=data.city, state=data.state,
        country=data.country.upper(), tier=data.tier, programs=programs, website=data.website, status="active",
    )
    db.add(row)
    await _flush_unique(db, data.name)
    if data.contact_name:
        db.add(
            InstitutionContact(
                id=uuid.uuid4(), tenant_id=row.tenant_id, institution_id=row.id, name=data.contact_name.strip(),
                title=data.contact_title, email=str(data.contact_email).lower() if data.contact_email else None,
                phone=phone, is_primary=True,
            )
        )
        await db.flush()
    return row.id
