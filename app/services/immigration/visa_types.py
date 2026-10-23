# ruff: file-ignore[line-too-long]
"""Visa Types (Epic 46 A1, FR-232, AD-52): adopt platform templates, create, version, patch, archive.

* Adopting copies the template definition into a tenant ``visa_types`` row + version 1 (409 ``visa_type_exists``
  when the key is taken; give a new name / key to adopt twice).
* Editing the definition appends a ``visa_type_versions`` row and moves ``current_version_id``; open Cases keep
  their pinned version.
* Adopting or editing provisions the P2 credential type ``permit.<key>`` with the definition's alert offsets.
"""

import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.immigration import ImmigrationCase, VisaType, VisaTypeTemplate, VisaTypeVersion
from app.schemas.immigration import (
    AdoptIn,
    VisaTemplateOut,
    VisaTypeCreate,
    VisaTypeDefinition,
    VisaTypeDetailOut,
    VisaTypeOut,
    VisaTypePatch,
    VisaTypeVersionOut,
)
from app.services.immigration import definition as defs
from app.services.immigration.common import (
    IMM_CONFIGURE,
    OPEN_STATUSES,
    conflict,
    not_found,
    now,
    require,
    tenant_of,
    user_id,
)


def credential_type_key(visa_key: str) -> str:
    return f"permit.{visa_key}"


# ================================================================ templates
async def list_templates(ctx: RequestContext, db: AsyncSession) -> list[VisaTemplateOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(VisaTypeTemplate).order_by(VisaTypeTemplate.name))).scalars().all()
    adopted = dict(
        (
            await db.execute(
                select(VisaType.template_key, VisaType.id).where(
                    VisaType.template_key.is_not(None), VisaType.archived_at.is_(None)
                )
            )
        ).tuples().all()
    )
    return [
        VisaTemplateOut(
            key=t.key,
            country=t.country,
            name=t.name,
            category=t.category,
            template_version=t.template_version,
            last_reviewed=t.last_reviewed,
            notice=t.notice,
            definition=VisaTypeDefinition.model_validate(t.definition),
            adopted_visa_type_id=adopted.get(t.key),
        )
        for t in rows
    ]


async def _template(db: AsyncSession, key: str) -> VisaTypeTemplate:
    t = (await db.execute(select(VisaTypeTemplate).where(VisaTypeTemplate.key == key))).scalar_one_or_none()
    if t is None:
        raise not_found("Visa type template")
    return t


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (s if s and s[0].isalpha() else f"v_{s}")[:50] or "visa_type"


async def _key_taken(db: AsyncSession, key: str) -> bool:
    return (await db.execute(select(VisaType.id).where(VisaType.key == key))).first() is not None


async def _new_version(
    ctx: RequestContext, db: AsyncSession, vt: VisaType, d: VisaTypeDefinition
) -> VisaTypeVersion:
    n = (
        await db.execute(
            select(func.coalesce(func.max(VisaTypeVersion.version_no), 0)).where(
                VisaTypeVersion.visa_type_id == vt.id
            )
        )
    ).scalar_one()
    v = VisaTypeVersion(
        id=uuid.uuid4(),
        tenant_id=vt.tenant_id,
        visa_type_id=vt.id,
        version_no=int(n) + 1,
        definition=defs.dump(d),
        created_by=user_id(ctx),
    )
    db.add(v)
    await db.flush()
    vt.current_version_id = v.id
    await db.flush()
    from app.services.immigration import permits

    await permits.ensure_credential_type(db, vt, d)
    return v


async def adopt(ctx: RequestContext, db: AsyncSession, template_key: str, data: AdoptIn) -> VisaTypeDetailOut:
    require(ctx, IMM_CONFIGURE)
    t = await _template(db, template_key)
    key = data.key or (_slug(data.name) if data.name else t.key)
    if await _key_taken(db, key):
        raise conflict("visa_type_exists", "This visa type was already adopted; give it a new name", key=key)
    vt = VisaType(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        key=key,
        country=t.country,
        name=data.name or t.name,
        category=t.category,
        template_key=t.key,
        template_version=t.template_version,
        last_reviewed=t.last_reviewed,
        active=True,
    )
    db.add(vt)
    await db.flush()
    await _new_version(ctx, db, vt, defs.validate(t.definition))
    audit.record("immigration.visa_type_adopted", "visa_type", vt.id, session=db, template=t.key)
    return await detail(ctx, db, vt.id)


async def create(ctx: RequestContext, db: AsyncSession, data: VisaTypeCreate) -> VisaTypeDetailOut:
    require(ctx, IMM_CONFIGURE)
    d = defs.validate(data.definition)
    if await _key_taken(db, data.key):
        raise conflict("visa_type_exists", "A visa type with this key exists", key=data.key)
    vt = VisaType(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        key=data.key,
        country=data.country,
        name=data.name,
        category=data.category,
        last_reviewed=data.last_reviewed,
        active=True,
    )
    db.add(vt)
    await db.flush()
    await _new_version(ctx, db, vt, d)
    return await detail(ctx, db, vt.id)


async def load(db: AsyncSession, visa_type_id: uuid.UUID) -> VisaType:
    vt = await db.get(VisaType, visa_type_id)
    if vt is None:
        raise not_found("Visa type")
    return vt


async def version(db: AsyncSession, version_id: uuid.UUID) -> tuple[VisaTypeVersion, VisaTypeDefinition]:
    v = await db.get(VisaTypeVersion, version_id)
    if v is None:  # pragma: no cover - FK
        raise not_found("Visa type version")
    return v, VisaTypeDefinition.model_validate(v.definition)


async def update_definition(
    ctx: RequestContext, db: AsyncSession, visa_type_id: uuid.UUID, raw: dict[str, Any]
) -> VisaTypeDetailOut:
    require(ctx, IMM_CONFIGURE)
    vt = await load(db, visa_type_id)
    if vt.archived_at is not None:
        raise conflict("visa_type_archived", "This visa type is archived")
    d = defs.validate(raw)
    v = await _new_version(ctx, db, vt, d)
    audit.record("immigration.visa_type_versioned", "visa_type", vt.id, session=db, version_no=v.version_no)
    return await detail(ctx, db, vt.id)


async def patch(
    ctx: RequestContext, db: AsyncSession, visa_type_id: uuid.UUID, data: VisaTypePatch
) -> VisaTypeDetailOut:
    require(ctx, IMM_CONFIGURE)
    vt = await load(db, visa_type_id)
    fields = data.model_dump(exclude_unset=True)
    if fields.get("name"):
        vt.name = fields["name"]
    if "active" in fields and fields["active"] is not None:
        vt.active = bool(fields["active"])
    if "last_reviewed" in fields:
        vt.last_reviewed = fields["last_reviewed"]
    await db.flush()
    if "name" in fields and vt.current_version_id:
        _, d = await version(db, vt.current_version_id)
        from app.services.immigration import permits

        await permits.ensure_credential_type(db, vt, d)
    return await detail(ctx, db, vt.id)


async def archive(ctx: RequestContext, db: AsyncSession, visa_type_id: uuid.UUID) -> VisaTypeDetailOut:
    require(ctx, IMM_CONFIGURE)
    vt = await load(db, visa_type_id)
    if vt.archived_at is None:
        vt.archived_at = now()
        vt.active = False
        await db.flush()
    return await detail(ctx, db, vt.id)


# ================================================================ reads
async def _open_cases(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await db.execute(
        select(ImmigrationCase.visa_type_id, func.count())
        .where(ImmigrationCase.visa_type_id.in_(ids), ImmigrationCase.status.in_(OPEN_STATUSES))
        .group_by(ImmigrationCase.visa_type_id)
    )
    return {k: int(n) for k, n in rows.all()}


async def _template_reviewed(db: AsyncSession) -> dict[str, Any]:
    return dict((await db.execute(select(VisaTypeTemplate.key, VisaTypeTemplate.last_reviewed))).tuples().all())


def _out(vt: VisaType, version_no: int | None, reviewed: dict[str, Any], open_cases: int) -> dict[str, Any]:
    return {
        "id": vt.id,
        "key": vt.key,
        "country": vt.country,
        "name": vt.name,
        "category": vt.category,
        "active": vt.active,
        "archived_at": vt.archived_at,
        "template_key": vt.template_key,
        "template_version": vt.template_version,
        "template_last_reviewed": reviewed.get(vt.template_key) if vt.template_key else None,
        "last_reviewed": vt.last_reviewed,
        "current_version_id": vt.current_version_id,
        "current_version_no": version_no,
        "credential_type_key": credential_type_key(vt.key),
        "open_cases": open_cases,
    }


async def list_types(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    include_archived: bool = False,
) -> list[VisaTypeOut]:
    stmt = select(VisaType).order_by(VisaType.name)
    if not include_archived:
        stmt = stmt.where(VisaType.archived_at.is_(None))
    rows = list((await db.execute(stmt)).scalars())
    versions = (
        dict(
            (
                await db.execute(
                    select(VisaTypeVersion.id, VisaTypeVersion.version_no).where(
                        VisaTypeVersion.id.in_([r.current_version_id for r in rows if r.current_version_id])
                    )
                )
            ).tuples().all()
        )
        if rows
        else {}
    )
    reviewed = await _template_reviewed(db)
    counts = await _open_cases(db, [r.id for r in rows])
    return [
        VisaTypeOut(**_out(r, versions.get(r.current_version_id), reviewed, counts.get(r.id, 0)))
        for r in rows
    ]


async def detail(ctx: RequestContext, db: AsyncSession, visa_type_id: uuid.UUID) -> VisaTypeDetailOut:  # ruff: ignore[unused-function-argument]
    vt = await load(db, visa_type_id)
    versions = list(
        (
            await db.execute(
                select(VisaTypeVersion)
                .where(VisaTypeVersion.visa_type_id == vt.id)
                .order_by(VisaTypeVersion.version_no.desc())
            )
        ).scalars()
    )
    current = next((v for v in versions if v.id == vt.current_version_id), versions[0] if versions else None)
    assert current is not None
    counts = await _open_cases(db, [vt.id])
    return VisaTypeDetailOut(
        **_out(vt, current.version_no, await _template_reviewed(db), counts.get(vt.id, 0)),
        definition=VisaTypeDefinition.model_validate(current.definition),
        versions=[
            VisaTypeVersionOut(
                id=v.id, version_no=v.version_no, created_by=v.created_by, created_at=v.created_at
            )
            for v in versions
        ],
    )
