# ruff: noqa: E501
"""Campus drives (Epic 43 story 43.1, FR-217): Institutions, Demands, batch, schedule, form, eligibility,
assessment, interview template and offer template.

States: ``draft -> registration_open`` (open-registration: form + >= 1 Institution + >= 1 Demand; issues one link +
QR per Institution) ``-> in_progress`` (close-registration, or the first assessment invite) ``-> closed``; any
non-closed ``-> cancelled`` (reason; revokes every link). Every other transition 409 ``transition_not_allowed``.

The campus Pool is Epic 38's ``campus_<batch_year>`` template (the drive page calls its enable endpoint); campus
stores no pool id.
"""

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.campus import CampusDrive, DriveDemand, DriveInstitution, Institution, Registration
from app.schemas.campus import (
    DemandRef,
    DriveIn,
    DriveInstitutionIn,
    DriveInstitutionOut,
    DriveInstitutionPatch,
    DriveListItem,
    DriveOut,
    DrivePatch,
)
from app.services.campus import forms, links
from app.services.campus.common import (
    conflict,
    demand_refs,
    drive_for,
    drive_scope,
    institution_names,
    invalid,
    now,
    registration_url,
    tenant_of,
    user_id_of,
    user_names,
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"registration_open", "cancelled"}),
    "registration_open": frozenset({"in_progress", "cancelled"}),
    "in_progress": frozenset({"closed", "cancelled"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}
EDITABLE = ("draft", "registration_open", "in_progress")
ACCEPTING = ("registration_open", "in_progress")


def check_transition(src: str, dst: str) -> None:
    if dst not in TRANSITIONS.get(src, frozenset()):
        raise conflict("transition_not_allowed", f"A {src.replace('_', ' ')} drive cannot move to {dst.replace('_', ' ')}", status=src, to=dst)


def _editable(drive: CampusDrive) -> None:
    if drive.status not in EDITABLE:
        raise conflict("drive_closed", "This drive is closed", status=drive.status)


# ================================================================ read models
async def drive_out(db: AsyncSession, drive: CampusDrive) -> DriveOut:
    dis = list(
        (await db.execute(select(DriveInstitution).where(DriveInstitution.drive_id == drive.id).order_by(DriveInstitution.created_at))).scalars()
    )
    names = await institution_names(db, [d.institution_id for d in dis])
    reg_counts = dict(
        (
            await db.execute(
                select(Registration.drive_institution_id, func.count())
                .where(Registration.drive_id == drive.id)
                .group_by(Registration.drive_institution_id)
            )
        ).tuples().all()
    )
    status_counts = dict(
        (
            await db.execute(
                select(Registration.status, func.count()).where(Registration.drive_id == drive.id).group_by(Registration.status)
            )
        ).tuples().all()
    )
    link_rows = await links.link_state(db, [d.registration_link_id for d in dis if d.registration_link_id])
    institutions: list[DriveInstitutionOut] = []
    for d in dis:
        link = link_rows.get(d.registration_link_id) if d.registration_link_id else None
        is_active = links.active(link)
        token = links.token_of(d.tenant_id, d.token_sealed) if is_active else None
        institutions.append(
            DriveInstitutionOut(
                id=d.id,
                institution_id=d.institution_id,
                institution_name=names.get(d.institution_id, "Institution"),
                status=d.status,  # type: ignore[arg-type]
                registration_cap=d.registration_cap,
                registration_url=registration_url(token) if token else None,
                link_expires_at=link.expires_at if link is not None else None,
                link_active=is_active,
                has_qr=d.qr_file_id is not None,
                registration_count=int(reg_counts.get(d.id, 0)),
            )
        )
    demand_ids = list((await db.execute(select(DriveDemand.demand_id).where(DriveDemand.drive_id == drive.id))).scalars())
    refs = await demand_refs(db, demand_ids)
    owners = await user_names(db, [drive.owner_id])
    assessment_name = None
    if drive.assessment_id is not None:
        from app.models.assessments import Assessment

        a = await db.get(Assessment, drive.assessment_id)
        assessment_name = a.name if a is not None else None
    return DriveOut(
        assessment_name=assessment_name,
        id=drive.id,
        name=drive.name,
        batch_year=drive.batch_year,
        status=drive.status,  # type: ignore[arg-type]
        owner_id=drive.owner_id,
        owner_name=owners.get(drive.owner_id),
        registration_opens_at=drive.registration_opens_at,
        registration_closes_at=drive.registration_closes_at,
        assessment_window=drive.assessment_window,
        interview_window=drive.interview_window,
        form_schema=drive.form_schema,
        eligibility_definition=drive.eligibility_definition,
        eligibility_revision=drive.eligibility_revision,
        assessment_id=drive.assessment_id,
        interview_stage_template_id=drive.interview_stage_template_id,
        offer_template_id=drive.offer_template_id,
        notes=drive.notes,
        cancel_reason=drive.cancel_reason,
        institutions=institutions,
        demands=[DemandRef(**refs[i]) for i in demand_ids if i in refs],
        status_counts={str(k): int(v) for k, v in status_counts.items()},
        created_at=drive.created_at,
        updated_at=drive.updated_at,
    )


async def list_drives(c: RequestContext, db: AsyncSession, *, q: str | None = None, status: str | None = None) -> list[DriveListItem]:
    stmt = select(CampusDrive)
    cond = drive_scope(c)
    if cond is not None:
        stmt = stmt.where(cond)
    if status:
        stmt = stmt.where(CampusDrive.status == status)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(CampusDrive.name.ilike(like, escape="\\"))
    rows = list((await db.execute(stmt.order_by(CampusDrive.created_at.desc()).limit(500))).scalars())
    ids = [r.id for r in rows]
    inst = dict((await db.execute(select(DriveInstitution.drive_id, func.count()).where(DriveInstitution.drive_id.in_(ids)).group_by(DriveInstitution.drive_id))).tuples().all()) if ids else {}
    regs = dict((await db.execute(select(Registration.drive_id, func.count()).where(Registration.drive_id.in_(ids)).group_by(Registration.drive_id))).tuples().all()) if ids else {}
    owners = await user_names(db, [r.owner_id for r in rows])
    return [
        DriveListItem(
            id=r.id, name=r.name, batch_year=r.batch_year, status=r.status, owner_id=r.owner_id,  # type: ignore[arg-type]
            owner_name=owners.get(r.owner_id), registration_closes_at=r.registration_closes_at,
            institution_count=int(inst.get(r.id, 0)), registration_count=int(regs.get(r.id, 0)), created_at=r.created_at,
        )
        for r in rows
    ]


async def get_drive(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID) -> DriveOut:
    return await drive_out(db, await drive_for(c, db, drive_id))


# ================================================================ create / update
async def _check_refs(db: AsyncSession, data: DriveIn | DrivePatch, fields: set[str]) -> None:
    from app.models.assessments import Assessment
    from app.models.identity import User
    from app.models.interviews import InterviewStageTemplate
    from app.models.offers import OfferTemplate

    checks: tuple[tuple[str, Any, str], ...] = (
        ("assessment_id", Assessment, "Assessment not found"),
        ("interview_stage_template_id", InterviewStageTemplate, "Interview template not found"),
        ("offer_template_id", OfferTemplate, "Offer template not found"),
        ("owner_id", User, "Owner not found"),
    )
    for key, model, message in checks:
        value = getattr(data, key, None)
        if key in fields and value is not None and await db.get(model, value) is None:
            raise invalid(f"unknown_{key.removesuffix('_id')}", message, {key: message})


def _window(opens: Any, closes: Any) -> None:
    if opens is not None and closes is not None and closes <= opens:
        raise invalid("invalid_window", "Registration must close after it opens", {"registration_closes_at": "Must be after the opening time"})


async def create_drive(c: RequestContext, db: AsyncSession, data: DriveIn) -> DriveOut:
    fields = set(data.model_fields_set)
    await _check_refs(db, data, fields | {"owner_id"})
    _window(data.registration_opens_at, data.registration_closes_at)
    owner = data.owner_id or user_id_of(c)
    if owner is None:
        raise invalid("owner_required", "A drive needs an owner", {"owner_id": "Required"})
    drive = CampusDrive(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name.strip(),
        batch_year=data.batch_year,
        status="draft",
        owner_id=owner,
        registration_opens_at=data.registration_opens_at,
        registration_closes_at=data.registration_closes_at,
        assessment_window=data.assessment_window,
        interview_window=data.interview_window,
        form_schema=forms.normalize(data.form_schema),
        assessment_id=data.assessment_id,
        interview_stage_template_id=data.interview_stage_template_id,
        offer_template_id=data.offer_template_id,
        notes=data.notes,
    )
    db.add(drive)
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


async def update_drive(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: DrivePatch) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    _editable(drive)
    changes = data.model_dump(exclude_unset=True)
    await _check_refs(db, data, set(changes))
    if "batch_year" in changes and changes["batch_year"] != drive.batch_year and drive.status != "draft":
        raise conflict("drive_not_draft", "The batch can only change while the drive is a draft")
    for key, value in changes.items():
        if key == "form_schema":
            drive.form_schema = forms.normalize(data.form_schema)
        elif key in ("name", "batch_year", "owner_id") and value is None:
            continue
        else:
            setattr(drive, key, value.strip() if key == "name" else value)
    _window(drive.registration_opens_at, drive.registration_closes_at)
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


# ================================================================ institutions / demands on a drive
async def add_institution(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: DriveInstitutionIn) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    if drive.status not in ("draft", "registration_open"):
        raise conflict("drive_not_open", "Institutions can be added before registration closes", status=drive.status)
    inst = await db.get(Institution, data.institution_id)
    if inst is None:
        raise NotFound("Institution not found")
    if inst.status != "active":
        raise conflict("institution_archived", "This institution is archived")
    exists = (
        await db.execute(select(DriveInstitution.id).where(DriveInstitution.drive_id == drive.id, DriveInstitution.institution_id == inst.id))
    ).first()
    if exists is not None:
        raise conflict("institution_on_drive", "This institution is already on the drive")
    di = DriveInstitution(
        id=uuid.uuid4(), tenant_id=drive.tenant_id, drive_id=drive.id, institution_id=inst.id,
        registration_cap=data.registration_cap, status="active",
    )
    db.add(di)
    await db.flush()
    if drive.status == "registration_open":
        await links.issue_drive_link(c, db, drive, di)
    await db.refresh(drive)
    return await drive_out(db, drive)


async def _drive_institution(db: AsyncSession, drive: CampusDrive, di_id: uuid.UUID) -> DriveInstitution:
    di = await db.get(DriveInstitution, di_id, with_for_update=True)
    if di is None or di.drive_id != drive.id:
        raise NotFound("Institution not found on this drive")
    return di


async def update_institution(
    c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, di_id: uuid.UUID, data: DriveInstitutionPatch
) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    _editable(drive)
    di = await _drive_institution(db, drive, di_id)
    changes = data.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] is not None:
        di.status = changes["status"]
    if "registration_cap" in changes:
        di.registration_cap = changes["registration_cap"]
        if di.registration_link_id is not None:
            from app.core.secure_links import SecureLink

            link = await db.get(SecureLink, di.registration_link_id)
            if link is not None:
                link.max_uses = di.registration_cap or links.MAX_USES
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


async def remove_institution(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, di_id: uuid.UUID) -> DriveOut:
    from app.core import secure_links

    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    _editable(drive)
    di = await _drive_institution(db, drive, di_id)
    used = (await db.execute(select(Registration.id).where(Registration.drive_institution_id == di.id).limit(1))).first()
    if used is not None:
        raise conflict("institution_has_registrations", "Students already registered through this institution; pause it instead")
    if di.registration_link_id is not None:
        await secure_links.revoke(db, di.registration_link_id)
    await db.delete(di)
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


async def add_demand(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, demand_id: uuid.UUID) -> DriveOut:
    from app.models.demands import Demand

    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    _editable(drive)
    demand = await db.get(Demand, demand_id)
    if demand is None or demand.archived_at is not None:
        raise NotFound("Demand not found")
    exists = (await db.execute(select(DriveDemand.demand_id).where(DriveDemand.drive_id == drive.id, DriveDemand.demand_id == demand_id))).first()
    if exists is not None:
        raise conflict("demand_on_drive", "This demand is already linked to the drive")
    db.add(DriveDemand(tenant_id=drive.tenant_id, drive_id=drive.id, demand_id=demand_id))
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


async def remove_demand(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, demand_id: uuid.UUID) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    _editable(drive)
    row = (await db.execute(select(DriveDemand).where(DriveDemand.drive_id == drive.id, DriveDemand.demand_id == demand_id))).scalar_one_or_none()
    if row is None:
        raise NotFound("Demand not linked to this drive")
    await db.delete(row)
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


async def drive_demand_ids(db: AsyncSession, drive_id: uuid.UUID) -> list[uuid.UUID]:
    return list((await db.execute(select(DriveDemand.demand_id).where(DriveDemand.drive_id == drive_id))).scalars())


# ================================================================ state machine
async def open_registration(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    check_transition(drive.status, "registration_open")
    dis = list((await db.execute(select(DriveInstitution).where(DriveInstitution.drive_id == drive.id))).scalars())
    missing: dict[str, str] = {}
    if not (drive.form_schema or {}).get("fields"):
        missing["form_schema"] = "Save the registration form"
    if not dis:
        missing["institutions"] = "Add at least one institution"
    if not await drive_demand_ids(db, drive.id):
        missing["demands"] = "Link at least one demand"
    if drive.registration_closes_at is not None and drive.registration_closes_at <= now():
        missing["registration_closes_at"] = "The closing time has passed"
    if missing:
        raise conflict("drive_incomplete", "The drive is not ready to open registration", fields=missing)
    drive.status = "registration_open"
    if drive.registration_opens_at is None:
        drive.registration_opens_at = now()
    for di in dis:
        await links.issue_drive_link(c, db, drive, di)
    await db.flush()
    audit.record("campus.registration_opened", "campus_drive", drive.id, session=db, institutions=len(dis))
    await db.refresh(drive)
    return await drive_out(db, drive)


async def mark_in_progress(db: AsyncSession, drive: CampusDrive) -> None:
    """``registration_open -> in_progress`` on the first assessment invite (registration stays open until it closes)."""
    if drive.status == "registration_open":
        drive.status = "in_progress"
        await db.flush()


async def close_registration(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    if drive.status == "in_progress":
        pass  # registration may still be open after the first invite: closing just revokes the links
    else:
        check_transition(drive.status, "in_progress")
        drive.status = "in_progress"
    drive.registration_closes_at = min(drive.registration_closes_at or now(), now())
    await links.revoke_drive_links(db, drive)
    await db.flush()
    audit.record("campus.registration_closed", "campus_drive", drive.id, session=db)
    await db.refresh(drive)
    return await drive_out(db, drive)


async def close_drive(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    check_transition(drive.status, "closed")
    drive.status = "closed"
    drive.closed_at = now()
    await links.revoke_drive_links(db, drive)
    await db.flush()
    await db.refresh(drive)
    return await drive_out(db, drive)


async def cancel_drive(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, reason: str) -> DriveOut:
    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    check_transition(drive.status, "cancelled")
    drive.status = "cancelled"
    drive.cancel_reason = reason.strip()
    drive.cancelled_at = now()
    await links.revoke_drive_links(db, drive)
    await db.flush()
    audit.record("campus.drive_cancelled", "campus_drive", drive.id, session=db)
    await db.refresh(drive)
    return await drive_out(db, drive)


# ================================================================ Epic 52 quick search
async def quick_search(c: RequestContext, db: AsyncSession, q: str, limit: int) -> list[Any]:
    from app.schemas.search import QuickHit

    term = (q or "").strip()[:100]
    if not term:
        return []
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    stmt = select(CampusDrive).where(or_(CampusDrive.name.ilike(like, escape="\\")))
    cond = drive_scope(c)
    if cond is not None:
        stmt = stmt.where(cond)
    rows = (await db.execute(stmt.order_by(CampusDrive.updated_at.desc()).limit(max(1, min(limit, 50))))).scalars()
    return [
        QuickHit(type="campus_drive", id=d.id, title=d.name, subtitle=f"Batch {d.batch_year} - {d.status.replace('_', ' ')}", url=f"/campus/drives/{d.id}")
        for d in rows
    ]
