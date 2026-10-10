# ruff: noqa: E501
"""HRMS field mappings per Installation x Legal Entity (Epic 71, story 71.1, spec A3).

* ``get`` returns the HRMS schema fields with their mapping, ``required`` and the Talent OS catalogue.
* ``put`` replaces the mapping set (catalogue keys and transforms validated; Darwinbox accepts custom keys).
* Sensitive rows (PAN, bank, Aadhaar reference) are off by default: ``enable_sensitive`` needs a human holding both
  ``hrms:sync`` and ``ledger:sync`` (403 ``sensitive_mapping_requires_finance``) and a purpose; audited without values.
* ``build_fields`` turns catalogue values into the HRMS payload (only enabled sensitive rows are sent).
"""

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.permissions import Principal
from app.models.hrms_sync import TRANSFORMS, HrmsFieldMapping
from app.models.legal_entities import LegalEntity
from app.schemas.hrms_sync import CatalogueFieldOut, HrmsFieldOut, MappingIn, MappingOut, MappingPageOut
from app.services import integrations_service
from app.services.connectors.hrms.darwinbox import CUSTOM_KEY_RE
from app.services.hrms_sync.catalogue import BY_KEY, CATALOGUE, SENSITIVE_KEYS
from app.services.hrms_sync.common import entity_install, now, require_installation

__all__ = ["build_fields", "disable_sensitive", "enable_sensitive", "field_hash", "get_mappings", "load_rows", "put_mappings", "schema_for"]

_CUSTOM = re.compile(CUSTOM_KEY_RE)


def _user(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


async def _entity(db: AsyncSession, legal_entity_id: uuid.UUID) -> LegalEntity:
    le = await db.get(LegalEntity, legal_entity_id)
    if le is None:
        raise NotFound("Legal entity not found")
    return le


async def schema_for(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> Any:
    ei = await entity_install(db, installation_id, legal_entity_id)
    if ei is None:
        raise NotFound("The connector does not serve this legal entity")
    adapter = await integrations_service.adapter_for(ctx, db, installation_id, config_id=ei.config_id)
    return await adapter.field_schema()


async def load_rows(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> list[HrmsFieldMapping]:
    return list(
        (
            await db.execute(
                select(HrmsFieldMapping)
                .where(HrmsFieldMapping.installation_id == installation_id, HrmsFieldMapping.legal_entity_id == legal_entity_id)
                .order_by(HrmsFieldMapping.hrms_field)
            )
        ).scalars()
    )


def _out(row: HrmsFieldMapping) -> MappingOut:
    return MappingOut.model_validate(row)


async def get_mappings(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> MappingPageOut:
    inst = await require_installation(db, installation_id, "hrms")
    await _entity(db, legal_entity_id)
    schema = await schema_for(ctx, db, installation_id, legal_entity_id)
    rows = {r.hrms_field: r for r in await load_rows(db, installation_id, legal_entity_id)}
    fields = [
        HrmsFieldOut(
            hrms_field=f.key, label=f.label, required=f.required, kind=f.kind,
            options=[{"value": v, "label": lab} for v, lab in f.options], custom=False,
            mapping=_out(rows[f.key]) if f.key in rows else None,
        )
        for f in schema.fields
    ]  # fmt: skip
    known = {f.key for f in schema.fields}
    for key, row in rows.items():
        if key not in known:
            fields.append(HrmsFieldOut(hrms_field=key, label=key, required=False, kind="text", options=[], custom=True, mapping=_out(row)))
    return MappingPageOut(
        installation_id=installation_id,
        legal_entity_id=legal_entity_id,
        connector_key=str(inst.connector_key),
        custom_fields_allowed=str(inst.connector_key) == "darwinbox",
        fields=fields,
        catalogue=[CatalogueFieldOut(key=c.key, label=c.label, sensitive=c.sensitive) for c in CATALOGUE],
    )


def _validate(items: Sequence[MappingIn], known: set[str], custom_ok: bool) -> None:
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"items[{i}]"
        if item.hrms_field in seen:
            errors[where] = "Duplicate HRMS field"
        seen.add(item.hrms_field)
        if item.hrms_field not in known and not (custom_ok and _CUSTOM.match(item.hrms_field)):
            errors[where] = "Unknown HRMS field"
        if item.local_field is None and item.constant is None:
            errors[where] = "Choose a Talent OS field or a fixed value"
        if item.local_field is not None and item.local_field not in BY_KEY:
            errors[where] = "Unknown Talent OS field"
        if item.transform not in TRANSFORMS:
            errors[where] = "Unknown transform"
        if item.transform == "lookup" and not item.lookup:
            errors[where] = "Add lookup values"
    if errors:
        raise ValidationFailed("The mapping is invalid", code="mapping_invalid", details={"fields": errors})


async def put_mappings(
    ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID, items: Sequence[MappingIn]
) -> MappingPageOut:
    inst = await require_installation(db, installation_id, "hrms")
    await _entity(db, legal_entity_id)
    schema = await schema_for(ctx, db, installation_id, legal_entity_id)
    _validate(items, {f.key for f in schema.fields}, str(inst.connector_key) == "darwinbox")
    existing = {r.hrms_field: r for r in await load_rows(db, installation_id, legal_entity_id)}
    keep_sensitive = {
        k: r for k, r in existing.items()
        if r.sensitive and any(i.hrms_field == k and i.local_field == r.local_field for i in items)
    }  # fmt: skip
    await db.execute(
        delete(HrmsFieldMapping).where(
            HrmsFieldMapping.installation_id == installation_id, HrmsFieldMapping.legal_entity_id == legal_entity_id
        )
    )
    for item in items:
        prev = keep_sensitive.get(item.hrms_field)
        db.add(
            HrmsFieldMapping(
                id=uuid.uuid4(), tenant_id=inst.tenant_id, installation_id=installation_id, legal_entity_id=legal_entity_id,
                hrms_field=item.hrms_field, local_field=item.local_field, constant=item.constant if item.local_field is None else None,
                transform=item.transform, lookup=dict(item.lookup or {}),
                sensitive=prev is not None, purpose=prev.purpose if prev else None,
                enabled_by=prev.enabled_by if prev else None, enabled_at=prev.enabled_at if prev else None,
            )
        )  # fmt: skip
    await db.flush()
    audit.record("hrms.mapping_saved", "app_installation", installation_id, session=db, legal_entity_id=str(legal_entity_id), fields=len(items))
    return await get_mappings(ctx, db, installation_id, legal_entity_id)


async def _mapping(db: AsyncSession, mapping_id: uuid.UUID) -> HrmsFieldMapping:
    row = await db.get(HrmsFieldMapping, mapping_id)
    if row is None:
        raise NotFound("Mapping not found")
    return row


async def enable_sensitive(ctx: RequestContext, db: AsyncSession, mapping_id: uuid.UUID, purpose: str) -> MappingOut:
    require_human(actor_from_ctx(ctx))
    p = ctx.principal
    if not (isinstance(p, Principal) and p.type == "user" and p.has("ledger:sync") and p.has("hrms:sync")):
        raise Forbidden("Only Finance can turn on sensitive fields", code="sensitive_mapping_requires_finance", required_permission="ledger:sync")
    row = await _mapping(db, mapping_id)
    if row.local_field not in SENSITIVE_KEYS:
        raise ValidationFailed("This field is not sensitive", code="not_sensitive")
    row.sensitive, row.purpose, row.enabled_by, row.enabled_at = True, purpose.strip()[:500], _user(ctx), now()
    await db.flush()
    audit.record("hrms.sensitive_mapping_enabled", "hrms_field_mapping", row.id, session=db, field=row.local_field, hrms_field=row.hrms_field, purpose=row.purpose)
    return _out(row)


async def disable_sensitive(ctx: RequestContext, db: AsyncSession, mapping_id: uuid.UUID) -> MappingOut:  # noqa: ARG001
    row = await _mapping(db, mapping_id)
    row.sensitive, row.purpose, row.enabled_by, row.enabled_at = False, None, None, None
    await db.flush()
    audit.record("hrms.sensitive_mapping_disabled", "hrms_field_mapping", row.id, session=db, field=row.local_field, hrms_field=row.hrms_field)
    return _out(row)


def _transform(value: Any, row: HrmsFieldMapping) -> Any:
    if value is None:
        return None
    t = row.transform
    if t == "date_iso":
        return value.isoformat() if isinstance(value, date) else str(value)
    if t == "date_dmy":
        return value.strftime("%d-%m-%Y") if isinstance(value, date) else str(value)
    if t == "upper":
        return str(value).upper()
    if t == "lower":
        return str(value).lower()
    if t == "lookup":
        return (row.lookup or {}).get(str(value))
    return value.isoformat() if isinstance(value, date) else value


def wanted_keys(rows: Sequence[HrmsFieldMapping]) -> set[str]:
    return {r.local_field for r in rows if r.local_field and (r.local_field not in SENSITIVE_KEYS or r.sensitive)}


def build_fields(rows: Sequence[HrmsFieldMapping], values: Mapping[str, Any], required: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """``(hrms field -> value, missing required HRMS fields)``. Disabled sensitive rows are never sent."""
    fields: dict[str, Any] = {}
    for row in rows:
        if row.local_field in SENSITIVE_KEYS and not row.sensitive:
            continue
        raw = row.constant if row.local_field is None else values.get(row.local_field)
        value = _transform(raw, row)
        if value not in (None, ""):
            fields[row.hrms_field] = value
    missing = [f for f in required if f not in fields]
    return fields, missing


def field_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]
