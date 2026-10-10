# ruff: noqa: ARG001, E501
"""Configuration (Epic 21 A1/A3, P2-8.1, FR-116): credential types, compliance requirements and settings.

CRUD is audited (``AuditedMixin`` + explicit events). Item refs into other epics' tables (``bgv_package_id``,
``document_template_id``) are soft uuids: an unknown ref is reported ``unresolved`` (AD-16, no FK). Creating or
changing a requirement starts a re-evaluation run of active placements (after commit).
"""

import uuid
from typing import Any

from sqlalchemy import column, select, table, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.core.identity_docs import assert_no_full_aadhaar
from app.models.compliance import ComplianceEvaluationRun, ComplianceRequirement, CredentialType
from app.schemas.compliance import (
    ComplianceSettingsOut,
    ComplianceSettingsUpdate,
    CredentialTypeIn,
    CredentialTypeOut,
    CredentialTypeUpdate,
    RequirementIn,
    RequirementItemOut,
    RequirementMatch,
    RequirementOut,
    RequirementUpdate,
)
from app.services.compliance.common import (
    ReevaluationRequested,
    conflict,
    get_settings_row,
    not_found,
    now,
    tenant_of,
)

# key, label, category, has_expiry, number_kind, requires_document (mirrors migration 208_01)
SEED_TYPES: tuple[tuple[str, str, str, bool, str, bool], ...] = (
    ("passport", "Passport", "passport", True, "encrypted", True),
    ("visa", "Visa / work permit", "visa_work_permit", True, "encrypted", True),
    ("pan_card", "PAN card", "identity", False, "encrypted", True),
    ("aadhaar", "Aadhaar", "identity", False, "aadhaar_last4", False),
    ("certification", "Certification", "certification", True, "none", True),
    ("professional_licence", "Professional licence", "professional_licence", True, "encrypted", True),
    ("insurance", "Insurance", "insurance", True, "none", True),
    ("client_badge", "Client badge", "client_document", True, "none", False),
)

_SOFT_TABLES = {"bgv_package": "bgv_packages", "policy_ack": "document_templates"}


async def seed_credential_types(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    for key, label, category, has_expiry, number_kind, requires_document in SEED_TYPES:
        await db.execute(
            pg_insert(CredentialType)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                key=key,
                label=label,
                category=category,
                has_expiry=has_expiry,
                number_kind=number_kind,
                requires_document=requires_document,
            )
            .on_conflict_do_nothing(constraint="uq_credential_types_key")
        )


async def ensure_types(db: AsyncSession) -> None:
    """Seed the default types for a tenant that has none (tenants provisioned before this epic / without hooks)."""
    if (await db.execute(select(CredentialType.id).limit(1))).first() is None:
        try:
            async with db.begin_nested():
                await seed_credential_types(db, tenant_of(db))
        except Exception:  # a worker session cannot write configuration
            return


# ================================================================ credential types
async def list_types(db: AsyncSession, *, include_archived: bool = False) -> list[CredentialTypeOut]:
    await ensure_types(db)
    stmt = select(CredentialType).order_by(CredentialType.label)
    if not include_archived:
        stmt = stmt.where(CredentialType.archived_at.is_(None))
    return [CredentialTypeOut.model_validate(t) for t in (await db.execute(stmt)).scalars()]


async def type_by_key(db: AsyncSession, key: str) -> CredentialType | None:
    return (await db.execute(select(CredentialType).where(CredentialType.key == key))).scalar_one_or_none()


async def create_type(ctx: RequestContext, db: AsyncSession, data: CredentialTypeIn) -> CredentialTypeOut:
    if await type_by_key(db, data.key) is not None:
        raise conflict("credential_type_exists", "A credential type with this key exists")
    t = CredentialType(id=uuid.uuid4(), tenant_id=tenant_of(db), **data.model_dump())
    db.add(t)
    await db.flush()
    return CredentialTypeOut.model_validate(t)


async def update_type(
    ctx: RequestContext,
    db: AsyncSession,
    type_id: uuid.UUID,
    data: CredentialTypeUpdate,
) -> CredentialTypeOut:
    t = await db.get(CredentialType, type_id)
    if t is None:
        raise not_found("Credential type")
    values = data.model_dump(exclude_unset=True)
    archived = values.pop("archived", None)
    if "alert_offsets" in values and values["alert_offsets"] is not None:
        if any(o < 0 or o > 365 for o in values["alert_offsets"]):
            raise ValidationFailed("Offsets must be between 0 and 365 days", code="invalid_offsets")
        values["alert_offsets"] = sorted(set(values["alert_offsets"]), reverse=True)
    for k, v in values.items():
        setattr(t, k, v)
    if archived is not None:
        t.archived_at = now() if archived else None
    await db.flush()
    return CredentialTypeOut.model_validate(t)


# ================================================================ requirements
async def _known_refs(db: AsyncSession, kind: str, refs: set[str]) -> set[str] | None:
    """Existing ids of a soft-referenced table, or None when the table does not exist (yet)."""
    name = _SOFT_TABLES.get(kind)
    if name is None or not refs:
        return None
    ids = []
    for r in refs:
        try:
            ids.append(uuid.UUID(r))
        except ValueError:
            continue
    if (await db.execute(text("SELECT to_regclass(:n)"), {"n": name})).scalar() is None:
        return None
    t = table(name, column("id"))
    async with db.begin_nested():
        found = (
            {str(x) for x in (await db.execute(select(t.c.id).where(t.c.id.in_(ids)))).scalars()}
            if ids
            else set()
        )
    return found


async def _unresolved(db: AsyncSession, reqs: list[ComplianceRequirement]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    await ensure_types(db)
    types = {t.key for t in (await db.execute(select(CredentialType))).scalars()}
    by_kind: dict[str, set[str]] = {}
    for r in reqs:
        for i in r.items or []:
            by_kind.setdefault(i["kind"], set()).add(str(i["ref"]))
    for ref in by_kind.get("credential", set()):
        if ref not in types:
            out.add(("credential", ref))
    for kind in ("bgv_package", "policy_ack"):
        refs = by_kind.get(kind, set())
        try:
            known = await _known_refs(db, kind, refs)
        except Exception:
            known = None
        for ref in refs:
            if known is not None and ref not in known:
                out.add((kind, ref))
            elif known is None:
                try:
                    uuid.UUID(ref)
                except ValueError:
                    out.add((kind, ref))
    return out


def _out(
    r: ComplianceRequirement, unresolved: set[tuple[str, str]], run_id: uuid.UUID | None = None
) -> RequirementOut:
    return RequirementOut(
        id=r.id,
        name=r.name,
        client_id=r.client_id,
        match=RequirementMatch.model_validate(r.match or {}),
        items=[
            RequirementItemOut(**i, unresolved=(i["kind"], str(i["ref"])) in unresolved)
            for i in r.items or []
        ],
        active=r.active,
        effective_from=r.effective_from,
        archived_at=r.archived_at,
        created_at=r.created_at,
        evaluation_run_id=run_id,
    )


async def list_requirements(
    db: AsyncSession, *, client_id: uuid.UUID | None = None, include_archived: bool = False
) -> list[RequirementOut]:
    stmt = select(ComplianceRequirement).order_by(ComplianceRequirement.name)
    if client_id is not None:
        stmt = stmt.where(ComplianceRequirement.client_id == client_id)
    if not include_archived:
        stmt = stmt.where(ComplianceRequirement.archived_at.is_(None))
    reqs = list((await db.execute(stmt)).scalars())
    unresolved = await _unresolved(db, reqs)
    return [_out(r, unresolved) for r in reqs]


async def get_requirement(db: AsyncSession, requirement_id: uuid.UUID) -> RequirementOut:
    r = await db.get(ComplianceRequirement, requirement_id)
    if r is None:
        raise not_found("Requirement")
    return _out(r, await _unresolved(db, [r]))


def _validate_items(items: list[Any]) -> None:
    for i in items:
        assert_no_full_aadhaar(i.label, i.ref)
        if i.kind in ("bgv_package", "policy_ack"):
            try:
                uuid.UUID(i.ref)
            except ValueError as exc:
                raise ValidationFailed(
                    f"{i.kind} items reference an id", code="invalid_item_ref", details={"ref": i.ref}
                ) from exc


async def _start_run(db: AsyncSession, requirement_id: uuid.UUID) -> uuid.UUID:
    run = ComplianceEvaluationRun(id=uuid.uuid4(), tenant_id=tenant_of(db), requirement_id=requirement_id)
    db.add(run)
    await db.flush()
    emit(db, ReevaluationRequested(tenant_id=run.tenant_id, requirement_id=requirement_id, run_id=run.id))
    return run.id


async def _check_client(db: AsyncSession, client_id: uuid.UUID | None) -> None:
    if client_id is None:
        return
    clients = table("clients", column("id"))
    if (await db.execute(select(clients.c.id).where(clients.c.id == client_id))).first() is None:
        raise not_found("Client")


async def create_requirement(ctx: RequestContext, db: AsyncSession, data: RequirementIn) -> RequirementOut:
    assert_no_full_aadhaar(data.name)
    _validate_items(data.items)
    await _check_client(db, data.client_id)
    r = ComplianceRequirement(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name,
        client_id=data.client_id,
        match=data.match.model_dump(),
        items=[i.model_dump() for i in data.items],
        active=data.active,
        effective_from=data.effective_from,
    )
    db.add(r)
    await db.flush()
    await db.refresh(r)
    run_id = await _start_run(db, r.id) if r.active else None
    return _out(r, await _unresolved(db, [r]), run_id)


async def update_requirement(
    ctx: RequestContext,
    db: AsyncSession,
    requirement_id: uuid.UUID,
    data: RequirementUpdate,
) -> RequirementOut:
    r = await db.get(ComplianceRequirement, requirement_id)
    if r is None:
        raise not_found("Requirement")
    values = data.model_dump(exclude_unset=True)
    if "name" in values and values["name"] is not None:
        assert_no_full_aadhaar(values["name"])
        r.name = values["name"]
    if data.items is not None:
        _validate_items(data.items)
        keys = [(i.kind, i.ref) for i in data.items]
        if len(keys) != len(set(keys)):
            raise ValidationFailed("An item (kind, ref) may appear only once", code="duplicate_item")
        r.items = [i.model_dump() for i in data.items]
    if data.match is not None:
        r.match = data.match.model_dump()
    if "active" in values and values["active"] is not None:
        r.active = values["active"]
    if "effective_from" in values:
        r.effective_from = values["effective_from"]
    if values.get("archived") is not None:
        r.archived_at = now() if values["archived"] else None
    await db.flush()
    await db.refresh(r)
    run_id = await _start_run(db, r.id) if r.active and r.archived_at is None else None
    return _out(r, await _unresolved(db, [r]), run_id)


# ================================================================ settings
async def get_settings(db: AsyncSession) -> ComplianceSettingsOut:
    return ComplianceSettingsOut.model_validate(await get_settings_row(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: ComplianceSettingsUpdate
) -> ComplianceSettingsOut:
    s = await get_settings_row(db)
    values = data.model_dump(exclude_unset=True, exclude_none=True)
    if "default_alert_offsets" in values:
        offs = values["default_alert_offsets"]
        if any(o < 0 or o > 365 for o in offs):
            raise ValidationFailed("Offsets must be between 0 and 365 days", code="invalid_offsets")
        values["default_alert_offsets"] = sorted(set(offs), reverse=True)
    for k, v in values.items():
        setattr(s, k, v)
    await db.flush()
    audit.record(
        "compliance.settings_updated", "compliance_settings", s.id, session=db, fields=sorted(values)
    )
    return ComplianceSettingsOut.model_validate(s)
