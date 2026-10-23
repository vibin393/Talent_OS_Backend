"""Placement creation (Epic 19 A3, P2-6.1, FR-104) and the non-commercial PATCH.

* ``create_from_offer`` is idempotent: it locks the offer (``FOR UPDATE``), returns the existing Placement of the
  offer, and resolves a concurrent insert (unique ``offer_id``) the same way.
* Defaults: the released Offer version supplies rates, currency, unit, burden, dates, location and employment
  type; Legal Entity = the Demand's Business Unit default, else the tenant default; GST registration by work
  state (else principal); commercials / vendor defaults come from wrapped downward calls -- a missing or
  unimplemented function leaves the field NULL and lists a setup item.
* The first Rate Segment starts at ``planned_start_date``; margin via ``margin.compute``.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.placements import PlacementCreated
from app.models.placements import Placement, RateSegment
from app.schemas.placements_api import PlacementCreateBody, PlacementUpdate
from app.services.placements import margin, status
from app.services.placements.common import DEFAULT_WORKER_TYPES, settings_row, stale
from app.services.placements.common import get_placement as get_placement

EMPLOYMENT_TO_ENGAGEMENT = {
    "contract": "contract",
    "contract_to_hire": "c2h",
    "permanent": "permanent",
    "freelance": "contract",
}


async def _try(db: AsyncSession, fn: Any, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
    """Wrap a downward call (AD-32): ``(available, result)``; ``NotImplementedError`` / absence -> unavailable."""
    if fn is None:
        return False, None
    try:
        async with db.begin_nested():
            return True, await fn(*args, **kwargs)
    except NotImplementedError:
        return False, None
    except (NotFound, ValidationFailed, ConflictError):
        return True, None
    except Exception:
        logger.exception("placement default lookup {} failed", getattr(fn, "__name__", fn))
        return False, None


async def default_legal_entity(db: AsyncSession, demand_id: uuid.UUID | None) -> uuid.UUID:
    from app.models.demands import Demand
    from app.models.identity import BusinessUnit
    from app.models.legal_entities import LegalEntity

    if demand_id is not None:
        le = (
            await db.execute(
                select(BusinessUnit.default_legal_entity_id)
                .join(Demand, Demand.business_unit_id == BusinessUnit.id)
                .where(Demand.id == demand_id)
            )
        ).scalar_one_or_none()
        if le is not None:
            return le
    le = (
        await db.execute(
            select(LegalEntity.id)
            .where(LegalEntity.status == "active")
            .order_by(LegalEntity.is_backfilled.desc(), LegalEntity.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if le is None:
        raise ValidationFailed("The tenant has no active legal entity", code="legal_entity_missing")
    return le


def worker_type_for(settings_map: dict[str, Any], employment_type: str | None, vendor: bool) -> str | None:
    if vendor:
        return "vendor_contractor"
    key = employment_type or "contract"
    if key in settings_map:
        return settings_map[key]  # type: ignore[no-any-return]
    alias = EMPLOYMENT_TO_ENGAGEMENT.get(key)
    if alias and alias in settings_map:
        return settings_map[alias]
    return DEFAULT_WORKER_TYPES.get(key, "employee")


async def _resolve_commercials(
    ctx: RequestContext,
    db: AsyncSession,
    p: Placement,
    seg: RateSegment,
    *,
    role: str | None,
    location: str | None,
) -> list[str]:
    from app.services import commercials_service, legal_entities_service

    items: list[str] = []
    ok, reg = await _try(
        db,
        getattr(legal_entities_service, "default_registration_for", None),
        ctx,
        db,
        p.legal_entity_id,
        p.work_state_code,
    )
    if ok:
        p.supplier_gst_registration_id = p.supplier_gst_registration_id or reg
    on = p.planned_start_date
    if p.contract_id is None:
        ok, contract = await _try(
            db,
            getattr(commercials_service, "active_contract_for", None),
            ctx,
            db,
            p.client_id,
            p.legal_entity_id,
            on,
        )
        if ok and contract is not None:
            p.contract_id = contract.id
    if p.contract_id is None:
        items.append("contract_missing")
    if p.po_id is None:
        ok, po = await _try(
            db,
            getattr(commercials_service, "default_po", None),
            ctx,
            db,
            p.client_id,
            p.legal_entity_id,
            p.contract_id,
            on,
        )
        if ok and po is not None:
            p.po_id = po.po_id
    if p.po_id is None:
        items.append("po_missing")
    checked = False
    if seg.bill_rate is not None:
        from app.schemas.commercials import RateCheckRequest

        req = RateCheckRequest(
            subject_kind="placement",
            subject_id=p.id,
            client_id=p.client_id,
            contract_id=p.contract_id,
            on_date=on,
            role=role,
            location=location,
            bill_rate=seg.bill_rate,
            vendor_rate=seg.vendor_rate,
            currency=seg.currency,
            rate_unit=seg.rate_unit,
        )
        ok, result = await _try(db, getattr(commercials_service, "validate_rate", None), ctx, db, req)
        if ok and result is not None:
            checked = True
            seg.rate_check = result.model_dump(mode="json")
            if result.matched_line_id is not None:
                p.rate_card_line_id = result.matched_line_id
    if not checked:
        items.append("rate_card_unchecked")
    return items


async def _vendor(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID | None
) -> tuple[bool, Any]:
    from app.services import vendors_service

    if application_id is None:
        return True, None
    return await _try(db, getattr(vendors_service, "sourcing_for_application", None), ctx, db, application_id)


def _segment(
    p: Placement,
    *,
    rate_unit: str,
    pay: Any,
    bill: Any,
    vendor: Any,
    burden: Any,
    vburden: Any,
    actor: ActorRef,
) -> RateSegment:
    amount, pct = margin.compute(
        bill_rate=bill,
        pay_rate=pay,
        burden_pct=burden,
        vendor_rate=vendor,
        vendor_burden_pct=vburden,
        currency=p.currency,
    )
    return RateSegment(
        id=uuid.uuid4(),
        tenant_id=p.tenant_id,
        placement_id=p.id,
        effective_from=p.planned_start_date,
        rate_unit=rate_unit,
        currency=p.currency,
        pay_rate=Decimal(str(pay)) if pay is not None else None,
        bill_rate=Decimal(str(bill)) if bill is not None else None,
        vendor_rate=Decimal(str(vendor)) if vendor is not None else None,
        burden_pct=Decimal(str(burden)) if burden is not None else None,
        vendor_burden_pct=Decimal(str(vburden)) if vburden is not None else None,
        margin_amount=amount,
        margin_pct=pct,
        status="active",
        created_by=actor.id if actor.kind == "user" else None,
        reason="initial",
    )


async def _finish(
    ctx: RequestContext,
    db: AsyncSession,
    p: Placement,
    seg: RateSegment | None,
    actor: ActorRef,
    *,
    role: str | None,
    location: str | None,
    extra_items: list[str],
) -> Placement:
    from app.services.placements import gates, workers

    settings = await settings_row(db, p.tenant_id)
    p.human_id = await next_human_id(db, "placement", settings.human_id_prefix)
    items = list(extra_items)
    if seg is not None:
        items += await _resolve_commercials(ctx, db, p, seg, role=role, location=location)
    else:
        items += ["rate_card_unchecked"]
    p.setup_items = sorted(set(items))
    db.add(p)
    await db.flush()
    if seg is not None:
        db.add(seg)
        await db.flush()
    await status.record_creation(db, p, actor)
    await workers.ensure_worker_profile(ctx, db, p.candidate_id, default_worker_type=p.worker_type)
    await gates.refresh(ctx, db, p)
    emit(
        db,
        PlacementCreated(
            tenant_id=p.tenant_id,
            placement_id=p.id,
            candidate_id=p.candidate_id,
            client_id=p.client_id,
            vendor_id=p.vendor_id,
            legal_entity_id=p.legal_entity_id,
            offer_id=p.offer_id,
            demand_id=p.demand_id,
        ),
    )
    return p


async def create_from_offer(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> Placement:
    from app.models.demands import Demand
    from app.models.offers import Offer, OfferVersion

    offer = (
        await db.execute(select(Offer).where(Offer.id == offer_id).with_for_update())
    ).scalar_one_or_none()
    if offer is None:
        raise NotFound("Offer not found")
    existing = (
        await db.execute(select(Placement).where(Placement.offer_id == offer_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    if offer.status != "accepted":
        raise ConflictError("Only an accepted offer becomes a placement", code="offer_not_accepted")
    version_id = offer.released_version_id or offer.current_version_id
    version = (await db.execute(select(OfferVersion).where(OfferVersion.id == version_id))).scalar_one()
    demand = (await db.execute(select(Demand).where(Demand.id == offer.demand_id))).scalar_one_or_none()
    settings = await settings_row(db, offer.tenant_id)
    actor = ActorRef("system", None)

    ok, sourcing = await _vendor(ctx, db, offer.application_id)
    vendor_id = getattr(sourcing, "vendor_id", None) if sourcing else None
    vendor_rate = getattr(sourcing, "vendor_rate", None) if sourcing else None
    items = [] if ok else ["vendor_unchecked"]
    engagement = EMPLOYMENT_TO_ENGAGEMENT.get(version.employment_type, "contract")
    p = Placement(
        id=uuid.uuid4(),
        tenant_id=offer.tenant_id,
        human_id="pending",
        candidate_id=offer.candidate_id,
        client_id=offer.client_id,
        vendor_id=vendor_id,
        application_id=offer.application_id,
        offer_id=offer.id,
        demand_id=offer.demand_id,
        legal_entity_id=await default_legal_entity(db, offer.demand_id),
        engagement_kind=engagement,
        worker_type=worker_type_for(
            dict(settings.worker_type_by_employment_type or {}),
            version.employment_type,
            vendor_id is not None,
        ),
        payee_kind="vendor" if vendor_id else "worker",
        status="pre_joining",
        planned_start_date=version.start_date,
        planned_end_date=version.end_date,
        currency=version.pay_currency,
        work_location_text=version.location,
        role_title=version.title,
        recruiter_user_id=offer.owner_id,
        account_manager_user_id=getattr(demand, "owner_id", None),
    )
    if p.worker_type is None and engagement != "permanent":
        p.worker_type = "employee"
    unit = version.pay_rate_unit if version.pay_rate_unit in ("hour", "day", "month", "fixed") else "month"
    seg = _segment(
        p,
        rate_unit=unit,
        pay=version.pay_rate,
        bill=version.bill_rate,
        vendor=vendor_rate,
        burden=version.burden_pct,
        vburden=None,
        actor=actor,
    )
    try:
        async with db.begin_nested():
            await _finish(
                ctx, db, p, seg, actor, role=version.title, location=version.location, extra_items=items
            )
    except IntegrityError:
        existing = (
            await db.execute(select(Placement).where(Placement.offer_id == offer_id))
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    return p


async def create_direct(ctx: RequestContext, db: AsyncSession, data: PlacementCreateBody) -> Placement:
    from app.services.placements.common import tenant_of

    actor = actor_from_ctx(ctx)
    tenant_id = tenant_of(ctx, db)
    settings = await settings_row(db, tenant_id)
    if data.engagement_kind != "permanent" and data.pay_rate is None and data.bill_rate is None:
        raise ValidationFailed("A pay or bill rate is required", code="rate_required")
    le = data.legal_entity_id or await default_legal_entity(db, data.demand_id)
    worker_type = data.worker_type or worker_type_for(
        dict(settings.worker_type_by_employment_type or {}), data.engagement_kind, data.vendor_id is not None
    )
    if worker_type is None and data.engagement_kind != "permanent":
        worker_type = "employee"
    p = Placement(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id="pending",
        candidate_id=data.candidate_id,
        client_id=data.client_id,
        vendor_id=data.vendor_id,
        application_id=data.application_id,
        demand_id=data.demand_id,
        contract_id=data.contract_id,
        po_id=data.po_id,
        legal_entity_id=le,
        engagement_kind=data.engagement_kind,
        worker_type=worker_type,
        payee_kind="vendor" if data.vendor_id else "worker",
        status="pre_joining",
        planned_start_date=data.planned_start_date,
        planned_end_date=data.planned_end_date,
        currency=data.currency,
        work_state_code=data.work_state_code,
        work_timezone=data.work_timezone,
        work_location_text=data.work_location_text,
        role_title=data.role_title,
        recruiter_user_id=data.recruiter_user_id,
        account_manager_user_id=data.account_manager_user_id or (actor.id if actor.kind == "user" else None),
    )
    if data.planned_end_date is not None and data.planned_end_date < data.planned_start_date:
        raise ValidationFailed("The end date is before the start date", code="invalid_dates")
    seg = None
    if data.pay_rate is not None or data.bill_rate is not None or data.vendor_rate is not None:
        seg = _segment(
            p,
            rate_unit=data.rate_unit,
            pay=data.pay_rate,
            bill=data.bill_rate,
            vendor=data.vendor_rate,
            burden=data.burden_pct,
            vburden=data.vendor_burden_pct,
            actor=actor,
        )
    if data.validate_only:
        async with db.begin_nested() as sp:
            await _finish(
                ctx, db, p, seg, actor, role=data.role_title, location=data.work_location_text, extra_items=[]
            )
            await sp.rollback()
        return p
    await _finish(
        ctx, db, p, seg, actor, role=data.role_title, location=data.work_location_text, extra_items=[]
    )
    if data.imported and data.actual_start_date is not None:
        # Go-live import (Epic 35): an engagement already running is recorded as active by its importer.
        await status.transition_placement(ctx, db, p, "active", actor, reason="imported")
        p.actual_start_date = data.actual_start_date
        await db.flush()
    return p


_PATCHABLE = (
    "account_manager_user_id",
    "recruiter_user_id",
    "manager_user_id",
    "client_manager_contact_id",
    "work_location_text",
    "work_state_code",
    "work_timezone",
    "holiday_calendar_id",
    "approval_route_ref",
    "role_title",
)


async def update_placement(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    data: PlacementUpdate,
) -> Placement:
    p = await get_placement(db, placement_id, lock=True)
    if p.lock_version != data.lock_version:
        raise stale()
    changes = data.model_dump(exclude_unset=True, exclude={"lock_version"})
    for key, value in changes.items():
        if key in _PATCHABLE:
            setattr(p, key, value)
    if changes:
        p.lock_version += 1
    await db.flush()
    await db.refresh(p)
    return p


def summary_worker_type(p: Placement) -> str:
    """Contract DTOs require a worker type; permanent placements (NULL) report ``employee``."""
    return p.worker_type or "employee"


def today_or(value: date | None) -> date:
    return value or date.today()
