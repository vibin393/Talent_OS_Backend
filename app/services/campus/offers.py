# ruff: file-ignore[line-too-long]
"""Campus offers, joining and reneging (Epic 43 story 43.6, FR-221).

* ``bulk_create`` -- registrations must be ``interviewed`` with their Application at ``selected``; an ``OfferCreate``
  is built from the drive's offer template with ``start_date = planned_joining_date`` (+ overrides) and passed to
  ``offers_service.create_bulk`` (shared additive helper: per item SAVEPOINT, per-item errors). Each Offer then
  follows its own P1 approval and release (no ``offer:approve`` / ``offer:release`` here). Registration -> ``offered``.
* Handlers: ``OfferAccepted`` -> ``accepted_at``; ``OfferDeclined`` / ``OfferExpired`` -> ``rejected``;
  ``PlacementActivated`` -> ``joined``; ``PlacementDropped`` -> ``reneged``.
* ``renege`` -- open offer: ``withdraw_offer(reason="Reneged: <note>")``; accepted offer with a Placement before
  joining: the P2 dropout path (reason ``declined``); registration -> ``reneged``. P1 offers stay terminal at
  ``accepted`` (no ``offers`` column is added).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.models.campus import Registration
from app.schemas.campus import BulkOfferItemOut, BulkOffersIn, BulkOffersOut, JoiningRow, RegistrationOut
from app.services.campus.common import candidate_facts, conflict, drive_for, institution_names, now
from app.services.campus.domain_events import CampusOffersCreated

OPEN_OFFER = ("draft", "pending_approval", "approved", "released", "viewed")


def withdrawal_reasons() -> list[str]:
    from app.services import offers_service

    return list(offers_service.WITHDRAWAL_REASON_PRESETS)


def _offer_create(drive: Any, data: BulkOffersIn) -> Any:
    from app.schemas.offers import MoneyIn, OfferCreate

    o = data.overrides
    fields: dict[str, Any] = {"start_date": data.planned_joining_date}
    if drive.offer_template_id is not None:
        fields["template_id"] = drive.offer_template_id
    if o is not None:
        if o.pay_rate is not None:
            fields["pay_rate"] = MoneyIn(amount=o.pay_rate, currency=o.pay_currency.upper())
        for key in ("pay_rate_unit", "employment_type", "expiry_date", "location"):
            value = getattr(o, key)
            if value is not None:
                fields[key] = value
    return OfferCreate(**fields)


async def bulk_create(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, data: BulkOffersIn) -> BulkOffersOut:
    from app.models.pipeline import Application, PipelineStage
    from app.services import offers_service
    from app.services.campus.registration import set_status

    drive = await drive_for(c, db, drive_id, lock=True)
    if drive.status not in ("registration_open", "in_progress"):
        raise conflict("drive_not_running", "Offers are made while the drive is running", status=drive.status)
    regs = {
        r.id: r
        for r in (
            await db.execute(
                select(Registration).where(Registration.id.in_(data.registration_ids), Registration.drive_id == drive.id).with_for_update()
            )
        ).scalars()
    }
    stages = dict(
        (
            await db.execute(
                select(Application.id, PipelineStage.key)
                .join(PipelineStage, PipelineStage.id == Application.stage_id)
                .where(Application.id.in_([r.application_id for r in regs.values() if r.application_id]))
            )
        ).tuples().all()
    )
    results: dict[uuid.UUID, BulkOfferItemOut] = {}
    items: list[tuple[uuid.UUID, Any]] = []
    by_app: dict[uuid.UUID, Registration] = {}
    terms = _offer_create(drive, data)
    for rid in dict.fromkeys(data.registration_ids):
        reg = regs.get(rid)
        if reg is None:
            results[rid] = BulkOfferItemOut(registration_id=rid, error_code="not_found", message="Registration not found")
        elif reg.status != "interviewed" or reg.application_id is None:
            results[rid] = BulkOfferItemOut(registration_id=rid, error_code="not_interviewed", message="Only interviewed students can get an offer")
        elif stages.get(reg.application_id) != "selected":
            results[rid] = BulkOfferItemOut(registration_id=rid, error_code="application_not_selected", message="Select the application first")
        else:
            items.append((reg.application_id, terms))
            by_app[reg.application_id] = reg
    created = 0
    for res in await offers_service.create_bulk(c, db, items):
        reg = by_app[res.application_id]
        if res.offer_id is None:
            results[reg.id] = BulkOfferItemOut(registration_id=reg.id, error_code=res.error_code, message=res.message)
            continue
        set_status(reg, "offered", "user")
        reg.offer_id = res.offer_id
        reg.planned_joining_date = data.planned_joining_date
        created += 1
        results[reg.id] = BulkOfferItemOut(registration_id=reg.id, offer_id=res.offer_id, offer_status=res.status)
    await db.flush()
    if created:
        emit(db, CampusOffersCreated(tenant_id=drive.tenant_id, drive_id=drive.id, owner_id=drive.owner_id, count=created))
    audit.record("campus.bulk_offers", "campus_drive", drive.id, session=db, created=created, failed=len(results) - created)
    ordered = [results[rid] for rid in dict.fromkeys(data.registration_ids)]
    return BulkOffersOut(created=created, failed=len(ordered) - created, results=ordered)


async def joining(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID) -> list[JoiningRow]:
    from app.models.offers import Offer
    from app.models.placements import Placement

    drive = await drive_for(c, db, drive_id)
    regs = list(
        (
            await db.execute(
                select(Registration).where(Registration.drive_id == drive.id, Registration.offer_id.is_not(None)).order_by(Registration.offered_at)
            )
        ).scalars()
    )
    offer_ids = [r.offer_id for r in regs if r.offer_id]
    offers = dict((await db.execute(select(Offer.id, Offer.status).where(Offer.id.in_(offer_ids)))).tuples().all()) if offer_ids else {}
    placements = {
        p.offer_id: p for p in (await db.execute(select(Placement).where(Placement.offer_id.in_(offer_ids)))).scalars()
    } if offer_ids else {}
    facts = await candidate_facts(db, [r.candidate_id for r in regs])
    names = await institution_names(db, [r.institution_id for r in regs])
    out: list[JoiningRow] = []
    for r in regs:
        p = placements.get(r.offer_id)
        out.append(
            JoiningRow(
                registration_id=r.id, candidate_id=r.candidate_id, candidate_name=facts.get(r.candidate_id, (None, None))[0],
                institution_name=names.get(r.institution_id), status=r.status, offer_id=r.offer_id,  # type: ignore[arg-type]
                offer_status=offers.get(r.offer_id), planned_joining_date=r.planned_joining_date,
                placement_id=p.id if p else None, placement_status=p.status if p else None,
                accepted_at=r.accepted_at, joined_at=r.joined_at, reneged_at=r.reneged_at,
            )
        )
    return out


async def renege(c: RequestContext, db: AsyncSession, registration_id: uuid.UUID, note: str) -> RegistrationOut:
    from app.models.offers import Offer
    from app.models.placements import Placement
    from app.services import offers_service
    from app.services.campus.registration import registration_for, registration_outs, set_status
    from app.services.placements.common import PRE_START
    from app.services.placements.joining import drop

    reg = await registration_for(c, db, registration_id, write=True, lock=True)
    if reg.status != "offered" or reg.offer_id is None:
        raise conflict("transition_not_allowed", "Only a student with an offer can renege", status=reg.status)
    offer = await db.get(Offer, reg.offer_id)
    text = f"Reneged: {note.strip()}"[:2000]
    if offer is not None and offer.status in OPEN_OFFER:
        await offers_service.withdraw_offer(c, db, offer.id, text)
    elif offer is not None and offer.status == "accepted":
        placement = (await db.execute(select(Placement).where(Placement.offer_id == offer.id))).scalar_one_or_none()
        if placement is not None and placement.status in PRE_START:
            await drop(c, db, placement.id, "declined", text)
        elif placement is not None and placement.status not in ("dropped",):
            raise conflict("placement_started", "The student already joined; use offboarding instead", placement_status=placement.status)
    set_status(reg, "reneged", "user")
    reg.note = text[:1000]
    await db.flush()
    audit.record("campus.reneged", "campus_registration", reg.id, session=db)
    return (await registration_outs(db, [reg]))[0]


# ================================================================ handlers (tasks)
async def _by_offer(db: AsyncSession, offer_id: uuid.UUID) -> Registration | None:
    return (await db.execute(select(Registration).where(Registration.offer_id == offer_id).with_for_update())).scalar_one_or_none()


async def on_offer_accepted(db: AsyncSession, offer_id: uuid.UUID) -> bool:
    reg = await _by_offer(db, offer_id)
    if reg is None or reg.accepted_at is not None:
        return False
    reg.accepted_at = now()
    await db.flush()
    return True


async def on_offer_closed(db: AsyncSession, offer_id: uuid.UUID) -> bool:
    from app.services.campus.registration import set_status

    reg = await _by_offer(db, offer_id)
    if reg is None or reg.status != "offered":
        return False
    set_status(reg, "rejected", "system")
    await db.flush()
    return True


async def _by_placement(db: AsyncSession, placement_id: uuid.UUID) -> Registration | None:
    from app.models.placements import Placement

    p = await db.get(Placement, placement_id)
    if p is None:
        return None
    cond = []
    if p.offer_id is not None:
        cond.append(Registration.offer_id == p.offer_id)
    if p.application_id is not None:
        cond.append(Registration.application_id == p.application_id)
    if not cond:
        return None
    from sqlalchemy import or_

    return (await db.execute(select(Registration).where(or_(*cond)).with_for_update().limit(1))).scalar_one_or_none()


async def on_placement_activated(db: AsyncSession, placement_id: uuid.UUID) -> bool:
    from app.services.campus.registration import set_status

    reg = await _by_placement(db, placement_id)
    if reg is None or reg.status != "offered":
        return False
    set_status(reg, "joined", "system")
    await db.flush()
    return True


async def on_placement_dropped(db: AsyncSession, placement_id: uuid.UUID) -> bool:
    from app.services.campus.registration import set_status

    reg = await _by_placement(db, placement_id)
    if reg is None or reg.status != "offered":
        return False
    set_status(reg, "reneged", "system")
    await db.flush()
    return True


async def on_interviewed(db: AsyncSession, application_id: uuid.UUID) -> bool:
    """Interview completed / stage outcome / Application selected -> ``shortlisted -> interviewed``."""
    from app.services.campus.registration import set_status

    reg = (
        await db.execute(select(Registration).where(Registration.application_id == application_id).with_for_update())
    ).scalar_one_or_none()
    if reg is None or reg.status != "shortlisted":
        return False
    set_status(reg, "interviewed", "system")
    await db.flush()
    return True
