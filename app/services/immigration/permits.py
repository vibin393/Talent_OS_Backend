# ruff: file-ignore[line-too-long]
"""Permits (Epic 46 A4, FR-235, AD-52/AD-35).

* ``record`` -- inside the * Approved outcome or as an amendment / renewal of an approved Case: validates the
  pinned version's expiry fields (422 ``permit_expiry_required``), seals the number (``number`` + last4 + blind
  index), supersedes the previous active Permit of the Case, writes ``case_events(permit_recorded)`` and emits
  ``PermitRecorded``. The P2 compliance handler turns that into a ``permit.<visa_type_key>`` Credential.
* ``ensure_credential_type`` -- the P2 ``credential_types`` row for a Visa Type (tenant configuration; carries the
  visa's alert offsets into the P2 expiry engine without any compliance schema change).
* ``link_credential`` -- back-link from P2 ``CredentialRecorded`` (``permits.credential_id``).
* Status is derived: ``superseded`` / ``future`` / ``expired`` / ``active``.
"""

import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.encrypted import set_identity_field
from app.core.encrypted import unmask as unmask_field
from app.core.events import emit
from app.core.permissions import forbidden
from app.events.immigration import PermitRecorded
from app.models.compliance import Credential, CredentialType
from app.models.immigration import CaseEvent, ImmigrationCase, Permit, VisaType
from app.schemas.immigration import PermitFacts, PermitIn, PermitOut, VisaTypeDefinition
from app.services.immigration.common import (
    IDENTITY_VIEW,
    IMM_MANAGE,
    actor_json,
    conflict,
    has,
    invalid,
    not_found,
    require,
    today,
)

_NUMBER_SEPARATORS = re.compile(r"[\s\-/.]")
_NUMBER_RE = re.compile(r"^[A-Z0-9]{1,30}$")


def credential_type_key(visa_key: str) -> str:
    return f"permit.{visa_key}"


# ================================================================ credential type provisioning
async def ensure_credential_type(db: AsyncSession, vt: VisaType, d: VisaTypeDefinition) -> None:
    from app.services.compliance.requirements import ensure_types

    await ensure_types(db)  # keep the P2 default types for tenants that had none yet
    values = {
        "label": f"{vt.name} permit"[:200],
        "category": "visa_work_permit",
        "has_expiry": True,
        "number_kind": "encrypted",
        "requires_document": False,
        "alert_offsets": list(d.alert_offsets),
    }
    await db.execute(
        pg_insert(CredentialType)
        .values(id=uuid.uuid4(), tenant_id=vt.tenant_id, key=credential_type_key(vt.key), **values)
        .on_conflict_do_update(constraint="uq_credential_types_key", set_={**values, "archived_at": None})
    )


# ================================================================ helpers
def normalise_number(raw: str) -> str:
    value = _NUMBER_SEPARATORS.sub("", raw or "").upper()
    if not _NUMBER_RE.match(value):
        raise invalid("invalid_number_format", "Use letters and digits only (at most 30)")
    return value


def status_of(p: Permit, on: date | None = None) -> str:
    on = on or today()
    if p.superseded_by_id is not None:
        return "superseded"
    if p.valid_from > on:
        return "future"
    if p.valid_to is not None and p.valid_to < on:
        return "expired"
    return "active"


def to_out(p: Permit) -> PermitOut:
    return PermitOut(
        id=p.id,
        case_id=p.case_id,
        candidate_id=p.candidate_id,
        visa_type_id=p.visa_type_id,
        country=p.country,
        permit_type=p.permit_type,
        number_last4=p.number_last4,
        valid_from=p.valid_from,
        valid_to=p.valid_to,
        expiry_values={k: date.fromisoformat(v) for k, v in (p.expiry_values or {}).items()},
        conditions=p.conditions,
        document_id=p.document_id,
        credential_id=p.credential_id,
        superseded_by_id=p.superseded_by_id,
        status=status_of(p),  # type: ignore[arg-type]
        created_at=p.created_at,
    )


def check_expiry(d: VisaTypeDefinition, data: PermitIn) -> dict[str, str]:
    """Validate the permit dates against the version's expiry fields; return the non-primary values."""
    others: dict[str, str] = {}
    known = {f.key: f for f in d.expiry_fields}
    for k, v in data.expiry_values.items():
        f = known.get(k)
        if f is None:
            raise invalid("permit_expiry_unknown", f"Unknown expiry field {k}", field=k)
        if not f.primary:
            others[k] = v.isoformat()
    primary = next(f for f in d.expiry_fields if f.primary)
    valid_to = data.valid_to or data.expiry_values.get(primary.key)
    if primary.required and valid_to is None:
        raise invalid("permit_expiry_required", f"Enter {primary.label}", field=primary.key)
    for f in d.expiry_fields:
        if not f.primary and f.required and f.key not in others:
            raise invalid("permit_expiry_required", f"Enter {f.label}", field=f.key)
    if valid_to is not None and valid_to < data.valid_from:
        raise invalid("invalid_dates", "The permit ends before it starts")
    return others


# ================================================================ record
async def record(
    ctx: RequestContext, db: AsyncSession, case: ImmigrationCase, data: PermitIn, d: VisaTypeDefinition
) -> Permit:
    others = check_expiry(d, data)
    primary = next(f for f in d.expiry_fields if f.primary)
    valid_to = data.valid_to or data.expiry_values.get(primary.key)
    vt = await db.get(VisaType, case.visa_type_id)
    assert vt is not None
    number = normalise_number(data.number)
    p = Permit(
        id=uuid.uuid4(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        candidate_id=case.candidate_id,
        visa_type_id=case.visa_type_id,
        country=case.country,
        permit_type=(data.permit_type or f"{vt.name} permit")[:200],
        valid_from=data.valid_from,
        valid_to=valid_to,
        expiry_values=others,
        conditions=(data.conditions or "").strip() or None,
        document_id=data.document_id,
        recorded_actor=actor_json(ctx),
    )
    set_identity_field(p, "number", number, case.tenant_id)
    db.add(p)
    await db.flush()
    for prev in (
        await db.execute(
            select(Permit).where(
                Permit.case_id == case.id, Permit.id != p.id, Permit.superseded_by_id.is_(None)
            )
        )
    ).scalars():
        prev.superseded_by_id = p.id
    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            candidate_id=case.candidate_id,
            kind="permit_recorded",
            actor=actor_json(ctx),
            payload={
                "permit_id": str(p.id),
                "permit_type": p.permit_type,
                "valid_from": p.valid_from.isoformat(),
                "valid_to": p.valid_to.isoformat() if p.valid_to else None,
                "number_last4": p.number_last4,
            },
        )
    )
    await db.flush()
    emit(
        db,
        PermitRecorded(
            tenant_id=case.tenant_id,
            permit_id=p.id,
            immigration_case_id=case.id,
            candidate_id=case.candidate_id,
            placement_id=case.placement_id,
            country=case.country,
            permit_type=p.permit_type,
            valid_from=p.valid_from,
            valid_to=p.valid_to,
            credential_type_key=credential_type_key(vt.key),
        ),
    )
    return p


async def record_amendment(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: PermitIn
) -> PermitOut:
    """``POST /immigration/cases/{id}/permits``: amendment / renewal of an approved Case (human only)."""
    from app.core.actors import require_human
    from app.services.immigration import cases as cases_svc

    require(ctx, IMM_MANAGE)
    require_human(cases_svc.actor_of(ctx))
    case = await cases_svc.load_scoped(ctx, db, case_id, write=True)
    if case.status != "approved":
        raise conflict("case_not_approved", "Permits are recorded on approved cases only")
    _, d = await cases_svc.pinned(db, case)
    p = await record(ctx, db, case, data, d)
    return to_out(p)


# ================================================================ reads / unmask
async def list_for_case(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> list[PermitOut]:
    from app.services.immigration import cases as cases_svc

    await cases_svc.load_scoped(ctx, db, case_id)
    rows = (
        await db.execute(select(Permit).where(Permit.case_id == case_id).order_by(Permit.created_at.desc()))
    ).scalars()
    return [to_out(p) for p in rows]


async def unmask(ctx: RequestContext, db: AsyncSession, permit_id: uuid.UUID) -> str:
    from app.services.immigration import cases as cases_svc

    p = await db.get(Permit, permit_id)
    if p is None:
        raise not_found("Permit")
    await cases_svc.load_scoped(ctx, db, p.case_id)
    if not has(ctx, IDENTITY_VIEW):
        raise forbidden(IDENTITY_VIEW)
    if p.number is None:
        raise conflict("nothing_to_unmask", "This permit has no stored number")
    value = await unmask_field(ctx, db, p, "number")
    audit.record(
        "immigration.permit_unmasked", "immigration_case", p.case_id, session=db, permit_id=str(p.id)
    )
    return value


async def permits_for(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    candidate_id: uuid.UUID,
    on_date: date,
) -> list[PermitFacts]:
    rows = (
        await db.execute(
            select(Permit, VisaType.key)
            .join(VisaType, VisaType.id == Permit.visa_type_id)
            .where(
                Permit.candidate_id == candidate_id,
                Permit.superseded_by_id.is_(None),
                Permit.valid_from <= on_date,
                (Permit.valid_to.is_(None)) | (Permit.valid_to >= on_date),
            )
            .order_by(Permit.valid_from)
        )
    ).all()
    return [
        PermitFacts(
            permit_id=p.id,
            case_id=p.case_id,
            candidate_id=p.candidate_id,
            country=p.country,
            permit_type=p.permit_type,
            visa_type_key=key,
            valid_from=p.valid_from,
            valid_to=p.valid_to,
            credential_id=p.credential_id,
        )
        for p, key in rows
    ]


# ================================================================ P2 back-link
async def link_credential(db: AsyncSession, credential_id: uuid.UUID) -> bool:
    """P2 ``CredentialRecorded`` of a ``permit.*`` type: set ``permits.credential_id`` on the matching Permit."""
    c = await db.get(Credential, credential_id)
    if c is None or not c.type_key.startswith("permit."):
        return False
    visa_key = c.type_key.removeprefix("permit.")
    stmt: Any = (
        select(Permit)
        .join(VisaType, VisaType.id == Permit.visa_type_id)
        .where(
            Permit.candidate_id == c.candidate_id,
            VisaType.key == visa_key,
            Permit.valid_from == c.issued_on,
            Permit.credential_id.is_(None),
        )
        .order_by(Permit.created_at.desc())
    )
    stmt = stmt.where(Permit.valid_to.is_(None) if c.expires_on is None else Permit.valid_to == c.expires_on)
    p = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if p is None:
        return False
    p.credential_id = c.id
    await db.flush()
    return True
