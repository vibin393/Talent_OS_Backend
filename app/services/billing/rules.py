# ruff: noqa: ARG001
"""Billing Rules, billing settings, Client billing settings, schedule / manual items and their posting sources
(FR-143, FR-147). Rule edits bump ``version`` (drafts keep the values they were priced with)."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.billing import BillingClientSettings, BillingRule, BillingScheduleItem, ManualBillingItem
from app.models.placements import Placement
from app.schemas.billing import (
    BillingRuleIn,
    BillingRuleOut,
    BillingSettingsOut,
    BillingSettingsUpdate,
    ClientBillingSettingsIn,
    ClientBillingSettingsOut,
    ManualItemIn,
    ManualItemOut,
)
from app.services.billing.common import invalid, not_found, settings, tenant_of
from app.services.postings import SourceItem, SourceQuery

SCHEDULE_SOURCE = "schedule_item"
MANUAL_SOURCE = "manual_item"
_BASIS = {"hour": "hourly", "day": "daily", "month": "monthly_fixed"}


# ---------------------------------------------------------------- settings
async def get_settings(ctx: RequestContext, db: AsyncSession) -> BillingSettingsOut:
    return BillingSettingsOut.model_validate(await settings(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: BillingSettingsUpdate
) -> BillingSettingsOut:
    row = await settings(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    return BillingSettingsOut.model_validate(row)


async def get_client_settings(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID
) -> ClientBillingSettingsOut:
    row = (
        await db.execute(select(BillingClientSettings).where(BillingClientSettings.client_id == client_id))
    ).scalar_one_or_none()
    if row is None:
        return ClientBillingSettingsOut(client_id=client_id, reverse_charge=False, annexure=False)
    return ClientBillingSettingsOut.model_validate(row)


async def put_client_settings(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientBillingSettingsIn
) -> ClientBillingSettingsOut:
    from app.models.clients import Client

    if (await db.execute(select(Client.id).where(Client.id == client_id))).first() is None:
        raise not_found("client")
    await db.execute(
        pg_insert(BillingClientSettings)
        .values(
            tenant_id=tenant_of(db),
            client_id=client_id,
            reverse_charge=data.reverse_charge,
            annexure=data.annexure,
        )
        .on_conflict_do_update(
            index_elements=["client_id"],
            set_={"reverse_charge": data.reverse_charge, "annexure": data.annexure},
        )
    )
    return await get_client_settings(ctx, db, client_id)


# ---------------------------------------------------------------- rules
async def _placement(db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    row = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if row is None:
        raise not_found("placement")
    return row


async def get_rule(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> BillingRuleOut | None:
    await _placement(db, placement_id)
    row = (
        await db.execute(select(BillingRule).where(BillingRule.placement_id == placement_id))
    ).scalar_one_or_none()
    return BillingRuleOut.model_validate(row) if row is not None else None


async def put_rule(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: BillingRuleIn
) -> BillingRuleOut:
    await _placement(db, placement_id)
    if data.expense_rebill == "markup" and data.markup_pct is None:
        raise invalid("markup_required", "A markup percentage is required")
    row = (
        await db.execute(
            select(BillingRule).where(BillingRule.placement_id == placement_id).with_for_update()
        )
    ).scalar_one_or_none()
    values = data.model_dump()
    if row is None:
        row = BillingRule(
            id=uuid.uuid4(), tenant_id=tenant_of(db), placement_id=placement_id, version=1, **values
        )
        db.add(row)
    else:
        changed = any(getattr(row, k) != v for k, v in values.items())
        for k, v in values.items():
            setattr(row, k, v)
        if changed:
            row.version = row.version + 1
    await db.flush()
    await db.refresh(row)
    return await get_rule(ctx, db, placement_id)  # type: ignore[return-value]


async def ensure_default_rule(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> BillingRuleOut | None:
    """``PlacementCreated``: a rule from the placement's rate unit (idempotent; never overwrites)."""
    p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if p is None:
        return None
    existing = await get_rule(ctx, db, placement_id)
    if existing is not None:
        return existing
    from app.services import placements_service

    basis = "hourly"
    try:
        seg = await placements_service.rate_segment_on(
            ctx, db, placement_id, p.actual_start_date or p.planned_start_date
        )
        if seg is not None:
            basis = _BASIS.get(str(seg.rate_unit), "hourly")
    except Exception:
        basis = "hourly"
    await db.execute(
        pg_insert(BillingRule)
        .values(id=uuid.uuid4(), tenant_id=tenant_of(db), placement_id=placement_id, basis=basis)
        .on_conflict_do_nothing(index_elements=["tenant_id", "placement_id"])
    )
    return await get_rule(ctx, db, placement_id)


# ---------------------------------------------------------------- manual / schedule items
async def create_manual_item(ctx: RequestContext, db: AsyncSession, data: ManualItemIn) -> ManualItemOut:
    if data.kind == "placement_fee" and data.offer_id is None:
        raise invalid("offer_required", "A placement fee needs its offer")
    if Decimal(data.quantity) <= 0 or Decimal(data.rate) < 0:
        raise invalid("negative_line", "Quantity must be positive and the rate not negative")
    row = ManualBillingItem(id=uuid.uuid4(), tenant_id=tenant_of(db), **data.model_dump())
    db.add(row)
    await db.flush()
    return ManualItemOut.model_validate(row)


async def add_schedule_item(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    kind: str,
    period_start: date,
    amount: Decimal,
    currency: str,
    description: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        pg_insert(BillingScheduleItem)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            placement_id=placement_id,
            kind=kind,
            period_start=period_start,
            amount=amount,
            currency=currency,
            description=description,
            payload=payload or {},
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "placement_id", "kind", "period_start"])
    )


async def load_schedule_items(ctx: RequestContext, db: AsyncSession, query: SourceQuery) -> list[SourceItem]:
    if query.ledger != "billing":
        return []
    stmt = (
        select(BillingScheduleItem, Placement.legal_entity_id, Placement.client_id)
        .join(Placement, Placement.id == BillingScheduleItem.placement_id)
        .where(BillingScheduleItem.period_start <= query.period_end)
    )
    if query.placement_ids is not None:
        stmt = stmt.where(BillingScheduleItem.placement_id.in_(list(query.placement_ids)))
    if query.legal_entity_id is not None:
        stmt = stmt.where(Placement.legal_entity_id == query.legal_entity_id)
    out = []
    for item, le, client_id in (await db.execute(stmt)).all():
        out.append(
            SourceItem(
                SCHEDULE_SOURCE,
                item.id,
                item.placement_id,
                item.period_start,
                Decimal(1),
                Decimal(item.amount),
                item.currency,
                {
                    "category": item.kind,
                    "billable": True,
                    "legal_entity_id": le,
                    "client_id": client_id,
                    "unit": "month" if item.kind == "monthly_fixed" else "item",
                    "description": item.description,
                    "period_start": item.period_start,
                    "period_end": item.period_end or item.period_start,
                },
            )
        )
    return out


async def load_manual_items(ctx: RequestContext, db: AsyncSession, query: SourceQuery) -> list[SourceItem]:
    if query.ledger != "billing":
        return []
    stmt = select(ManualBillingItem).where(ManualBillingItem.item_date <= query.period_end)
    if query.legal_entity_id is not None:
        stmt = stmt.where(ManualBillingItem.legal_entity_id == query.legal_entity_id)
    out = []
    for item in (await db.execute(stmt)).scalars():
        if query.placement_ids is not None and item.placement_id not in set(query.placement_ids):
            continue
        out.append(
            SourceItem(
                MANUAL_SOURCE,
                item.id,
                item.placement_id,
                item.item_date,
                Decimal(item.quantity),
                Decimal(item.quantity) * Decimal(item.rate),
                item.currency,
                {
                    "category": item.kind,
                    "billable": True,
                    "legal_entity_id": item.legal_entity_id,
                    "client_id": item.client_id,
                    "unit": "item",
                    "description": item.description,
                    "quantity": Decimal(item.quantity),
                    "rate": Decimal(item.rate),
                    "sac_code": item.sac_code,
                    "po_id": item.po_id,
                },
            )
        )
    return out


def register_sources() -> None:
    from app.services.postings import posting_sources, register_posting_source

    billing = frozenset({"billing"})
    if SCHEDULE_SOURCE not in posting_sources():
        register_posting_source(SCHEDULE_SOURCE, load_schedule_items, ledgers=billing)  # type: ignore[arg-type]
    if MANUAL_SOURCE not in posting_sources():
        register_posting_source(MANUAL_SOURCE, load_manual_items, ledgers=billing)  # type: ignore[arg-type]
