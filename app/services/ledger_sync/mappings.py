# ruff: noqa: E501, ARG001, C416
"""Ledger mappings per Installation x Legal Entity (Epic 71, story 71.5, spec A8): revenue (billing line category or
``*``), GST output (``<component>:<rate>``, e.g. ``cgst:9``), TDS receivable, round-off, customers
(``<client_id>`` or ``<client_id>:<billing_profile_id>``), items (SAC or ``*``), bank accounts and discount.
"Load from Zoho Books" reads the ledger pickers (``masters_sync([], [])``)."""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.billing import InvoiceLine, SacCode
from app.models.clients import Client
from app.models.ledger_sync import LedgerMapping
from app.models.legal_entities import LegalEntity, LegalEntityBankAccount
from app.schemas.ledger_sync import LedgerMappingIn, LedgerMappingOut, LedgerMappingPageOut, MastersOut
from app.services import integrations_service
from app.services.ledger_sync.common import entity_install, require_installation

__all__ = ["MappingSet", "get_mappings", "load_masters", "mapping_set", "put_mappings", "rate_key", "save_mapping"]


def rate_key(component: str, rate: Decimal | float | str) -> str:
    """``cgst:9``, ``sgst:2.5``, ``igst:18`` (trailing zeros dropped)."""
    value = Decimal(str(rate)).normalize()
    text = format(value, "f")
    return f"{component.lower()}:{text}"


class MappingSet:
    def __init__(self, rows: Sequence[LedgerMapping]) -> None:
        self._map = {(r.kind, r.key): r.external_ref for r in rows}

    def get(self, kind: str, key: str | None, *, wildcard: bool = True) -> str | None:
        if key is not None and (kind, key) in self._map:
            return self._map[kind, key]
        return self._map.get((kind, "*")) if wildcard else None


async def load_rows(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> list[LedgerMapping]:
    return list(
        (
            await db.execute(
                select(LedgerMapping)
                .where(LedgerMapping.installation_id == installation_id, LedgerMapping.legal_entity_id == legal_entity_id)
                .order_by(LedgerMapping.kind, LedgerMapping.key)
            )
        ).scalars()
    )


async def mapping_set(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> MappingSet:
    return MappingSet(await load_rows(db, installation_id, legal_entity_id))


async def _entity(db: AsyncSession, legal_entity_id: uuid.UUID) -> LegalEntity:
    le = await db.get(LegalEntity, legal_entity_id)
    if le is None:
        raise NotFound("Legal entity not found")
    return le


async def get_mappings(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> LedgerMappingPageOut:
    inst = await require_installation(db, installation_id, "accounting")
    await _entity(db, legal_entity_id)
    rows = await load_rows(db, installation_id, legal_entity_id)
    categories = sorted({c for c in (await db.execute(select(InvoiceLine.category).distinct())).scalars() if c})
    clients = [{"id": str(cid), "name": str(name)} for cid, name in (await db.execute(select(Client.id, Client.name).order_by(Client.name).limit(500))).all()]
    sacs = sorted({c for c in (await db.execute(select(SacCode.code).where(SacCode.active.is_(True)))).scalars()})
    banks = [
        {"id": str(b.id), "name": f"{b.label} ({b.bank_name})"}
        for b in (await db.execute(select(LegalEntityBankAccount).where(LegalEntityBankAccount.legal_entity_id == legal_entity_id))).scalars()
    ]
    return LedgerMappingPageOut(
        installation_id=installation_id, legal_entity_id=legal_entity_id, connector_key=str(inst.connector_key),
        items=[LedgerMappingOut.model_validate(r) for r in rows], categories=categories, clients=clients, sac_codes=sacs, bank_accounts=banks,
    )  # fmt: skip


async def put_mappings(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID, items: Sequence[LedgerMappingIn]) -> LedgerMappingPageOut:
    inst = await require_installation(db, installation_id, "accounting")
    await _entity(db, legal_entity_id)
    seen: set[tuple[str, str]] = set()
    for item in items:
        if (item.kind, item.key) in seen:
            raise ValidationFailed("Duplicate mapping", code="mapping_invalid", details={"fields": {f"{item.kind}:{item.key}": "Duplicate"}})
        seen.add((item.kind, item.key))
    await db.execute(delete(LedgerMapping).where(LedgerMapping.installation_id == installation_id, LedgerMapping.legal_entity_id == legal_entity_id))
    for item in items:
        db.add(
            LedgerMapping(
                id=uuid.uuid4(), tenant_id=inst.tenant_id, installation_id=installation_id, legal_entity_id=legal_entity_id,
                kind=item.kind, key=item.key.strip(), external_ref=item.external_ref.strip(), external_label=item.external_label,
            )
        )  # fmt: skip
    await db.flush()
    audit.record("ledger.mapping_saved", "app_installation", installation_id, session=db, legal_entity_id=str(legal_entity_id), rows=len(items))
    return await get_mappings(ctx, db, installation_id, legal_entity_id)


async def save_mapping(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID, kind: str, key: str, external_ref: str, label: str | None = None) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.services.ledger_sync.common import tenant_of

    stmt = pg_insert(LedgerMapping).values(
        id=uuid.uuid4(), tenant_id=tenant_of(db), installation_id=installation_id, legal_entity_id=legal_entity_id,
        kind=kind, key=key, external_ref=external_ref[:200], external_label=(label or None) and label[:200],
    )  # fmt: skip
    await db.execute(stmt.on_conflict_do_update(constraint="uq_ledger_mappings_key", set_={"external_ref": stmt.excluded.external_ref}))


async def load_masters(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> MastersOut:
    await require_installation(db, installation_id, "accounting")
    ei = await entity_install(db, installation_id, legal_entity_id)
    if ei is None:
        raise NotFound("The connector does not serve this legal entity")
    adapter = await integrations_service.adapter_for(ctx, db, installation_id, config_id=ei.config_id)
    options = await adapter.masters_sync([], [])
    return MastersOut(options={str(k): str(v) for k, v in dict(options).items()})
