"""Offer status machine (PRD §5.3, AD-4) and ``transition_offer`` -- the only writer of ``offers.status``.

``transition_offer`` validates the move against ``TRANSITIONS``, writes ``offer_status_history``, audits (the
``offers`` row is audited automatically, attributed to the actor), emits ``OfferStatusChanged`` and applies the
pipeline / demand side effects in the caller's transaction:

* -> ``released``: application ``selected -> offered`` (system); demand ``offers_released`` count.
* -> ``accepted``: application ``offered -> hired`` (system); ``apply_positions_filled`` with the number of
  accepted offers on the demand (Open -> Filled at ``positions``).
* -> ``declined`` / ``expired`` / ``withdrawn``: application ``offered -> selected`` (system) when it is
  Offered (e.g. withdrawn after release, or after a revision of a released offer); ``offers_released`` count.
* leaving ``released|viewed`` revokes the offer's open response links.

``ai``, ``workflow`` and ``api_key`` actors appear in no row (FR-65).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.offers import OfferAccepted, OfferDeclined, OfferExpired, OfferStatusChanged
from app.models.offers import Offer, OfferStatusHistory, OfferVersion
from app.services.offers.common import (
    LINK_PURPOSE,
    OPEN_FOR_RESPONSE,
    SUBJECT_TYPE,
    ActorKind,
    acting_as,
    actor_id,
    conflict,
    now,
)

_USER = frozenset({"user"})
_CANDIDATE = frozenset({"candidate"})
_SYSTEM = frozenset({"system"})

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("draft", "pending_approval"): _USER,
    ("draft", "approved"): _USER,
    ("pending_approval", "approved"): _USER,
    ("pending_approval", "draft"): _USER,
    ("approved", "released"): _USER,
    ("released", "viewed"): _CANDIDATE,
    ("released", "accepted"): _CANDIDATE,
    ("viewed", "accepted"): _CANDIDATE,
    ("released", "declined"): _CANDIDATE,
    ("viewed", "declined"): _CANDIDATE,
    ("released", "expired"): _SYSTEM,
    ("viewed", "expired"): _SYSTEM,
    ("approved", "draft"): _USER,
    ("released", "draft"): _USER,
    ("viewed", "draft"): _USER,
    ("draft", "withdrawn"): _USER,
    ("pending_approval", "withdrawn"): _USER,
    ("approved", "withdrawn"): _USER,
    ("released", "withdrawn"): _USER,
    ("viewed", "withdrawn"): _USER,
}
STATUSES: tuple[str, ...] = (
    "draft",
    "pending_approval",
    "approved",
    "released",
    "viewed",
    "accepted",
    "declined",
    "expired",
    "withdrawn",
)
NEVER_ALLOWED: frozenset[str] = frozenset({"ai", "workflow", "api_key", "agent", "app"})  # +agent (53), +app (66)
PIPELINE_REVERT = ("declined", "expired", "withdrawn")
COUNTED = ("released", "viewed", "accepted")


def allowed_actors(from_status: str, to_status: str) -> frozenset[str] | None:
    return TRANSITIONS.get((from_status, to_status))


def check_transition(from_status: str, to_status: str, actor: str) -> None:
    """409 ``human_only_action`` for automated actors, 409 ``invalid_transition`` otherwise."""
    if actor in NEVER_ALLOWED:
        raise conflict(
            "human_only_action",
            "Only a person (or the candidate) can change an offer's status",
            actor=actor,
            from_status=from_status,
            to_status=to_status,
        )
    allowed = allowed_actors(from_status, to_status)
    if allowed is None or actor not in allowed:
        raise conflict(
            "invalid_transition",
            "This offer status change is not allowed",
            from_status=from_status,
            to_status=to_status,
            actor=actor,
        )


async def _version_no(db: AsyncSession, version_id: uuid.UUID | None) -> int | None:
    if version_id is None:
        return None
    return (await db.execute(select(OfferVersion.version_no).where(OfferVersion.id == version_id))).scalar()


async def revoke_open_links(db: AsyncSession, offer_id: uuid.UUID) -> int:
    """Revoke the offer's response links that were never used (a consumed link keeps its ``used`` state)."""
    revoked = 0
    for link in await secure_links.links_for_subject(db, SUBJECT_TYPE, offer_id):
        if link.purpose == LINK_PURPOSE and link.revoked_at is None and link.consumed_at is None:
            revoked += int(await secure_links.revoke(db, link.id))
    return revoked


async def offers_released_count(db: AsyncSession, demand_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Offer)
                .where(Offer.demand_id == demand_id, Offer.status.in_(COUNTED))
            )
        ).scalar_one()
    )


async def accepted_count(db: AsyncSession, demand_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Offer)
                .where(
                    Offer.demand_id == demand_id,
                    Offer.status == "accepted",
                    # Epic 19: an offer whose placement was dropped no longer fills a position.
                    Offer.placement_dropped_at.is_(None),
                )
            )
        ).scalar_one()
    )


async def record_placement_dropped(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> None:
    """Epic 19 (note 18): the placement made from ``offer_id`` was dropped. Stamps ``placement_dropped_at`` and
    refreshes the demand's filled count (``apply_positions_filled``). The demand is never reopened here."""
    from datetime import UTC, datetime

    from app.services import demands as demands_service

    offer = (
        await db.execute(select(Offer).where(Offer.id == offer_id).with_for_update())
    ).scalar_one_or_none()
    if offer is None:
        raise NotFound("Offer not found")
    if offer.placement_dropped_at is None:
        offer.placement_dropped_at = datetime.now(UTC)
        await db.flush()
    await demands_service.apply_positions_filled(
        ctx, db, offer.demand_id, await accepted_count(db, offer.demand_id)
    )


async def _application_stage(db: AsyncSession, application_id: uuid.UUID) -> str | None:
    from app.models.pipeline import Application, PipelineStage

    return (
        await db.execute(
            select(PipelineStage.key)
            .join(Application, Application.stage_id == PipelineStage.id)
            .where(Application.id == application_id)
        )
    ).scalar()


async def _move_application(
    ctx: RequestContext, db: AsyncSession, offer: Offer, to_key: str, *, expected_from: str, strict: bool
) -> None:
    from app.services import pipeline_service

    current = await _application_stage(db, offer.application_id)
    if current == to_key:
        return
    if current != expected_from:
        if strict:
            raise conflict(
                "application_stage_changed", f"The application is no longer {expected_from}", stage=current
            )
        logger.info(
            "Offer {}: application {} is in {} (not {}); stage left unchanged",
            offer.id,
            offer.application_id,
            current,
            expected_from,
        )
        return
    await pipeline_service.transition_application(ctx, db, offer.application_id, to_key, "system")


async def _sync_released_count(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> None:
    from app.services import demands as demands_service

    await db.flush()
    await demands_service.record_pipeline_counts(
        ctx, db, demand_id, offers_released=await offers_released_count(db, demand_id)
    )


async def _side_effects(
    ctx: RequestContext, db: AsyncSession, offer: Offer, from_status: str, to: str
) -> None:
    from app.services import demands as demands_service

    if from_status in OPEN_FOR_RESPONSE and to not in OPEN_FOR_RESPONSE:
        await revoke_open_links(db, offer.id)
    if to == "released":
        await _move_application(ctx, db, offer, "offered", expected_from="selected", strict=True)
        await _sync_released_count(ctx, db, offer.demand_id)
    elif to == "accepted":
        await _move_application(ctx, db, offer, "hired", expected_from="offered", strict=True)
        await db.flush()
        await demands_service.apply_positions_filled(
            ctx, db, offer.demand_id, await accepted_count(db, offer.demand_id)
        )
        await _sync_released_count(ctx, db, offer.demand_id)
    elif to in PIPELINE_REVERT:
        # Also after a revision (released -> draft -> withdrawn) the application may still be Offered.
        await _move_application(ctx, db, offer, "selected", expected_from="offered", strict=False)
        await _sync_released_count(ctx, db, offer.demand_id)
    elif to == "draft" and from_status in OPEN_FOR_RESPONSE:
        await _sync_released_count(ctx, db, offer.demand_id)


async def transition_offer(
    ctx: RequestContext,
    db: AsyncSession,
    offer: Offer | uuid.UUID,
    to: str,
    actor: ActorKind | str,
    reason: str | None = None,
    *,
    actor_ref: uuid.UUID | None = None,
) -> Offer:
    """Move ``offer`` to ``to`` as ``actor`` (409 ``invalid_transition`` / ``human_only_action``).

    ``actor_ref`` identifies a candidate actor (defaults to the principal id for users)."""
    if to not in STATUSES:
        raise conflict("invalid_transition", "Unknown offer status", to_status=to)
    if not isinstance(offer, Offer):
        row = (
            await db.execute(select(Offer).where(Offer.id == offer).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("Offer not found")
        offer = row
    from_status = offer.status
    check_transition(from_status, to, str(actor))
    at = now()
    who = actor_ref if actor_ref is not None else (actor_id(ctx) if actor == "user" else None)
    version_no = await _version_no(db, offer.current_version_id)
    with acting_as(db, str(actor), who):
        offer.status = to
        offer.lock_version = offer.lock_version + 1
        if to == "viewed" and offer.viewed_at is None:
            offer.viewed_at = at
        if to in ("accepted", "declined"):
            offer.responded_at = at
        db.add(
            OfferStatusHistory(
                id=uuid.uuid4(),
                tenant_id=offer.tenant_id,
                offer_id=offer.id,
                from_status=from_status,
                to_status=to,
                actor_kind=str(actor),
                actor_id=who,
                version_no=version_no,
                reason=reason,
                at=at,
            )
        )
        await db.flush()
    await _side_effects(ctx, db, offer, from_status, to)
    common: dict[str, Any] = {
        "tenant_id": offer.tenant_id,
        "offer_id": offer.id,
        "application_id": offer.application_id,
        "demand_id": offer.demand_id,
        "candidate_id": offer.candidate_id,
    }
    emit(
        db,
        OfferStatusChanged(
            **common, from_status=from_status, to_status=to, actor_kind=str(actor), version_no=version_no
        ),
    )
    if to == "accepted":
        emit(db, OfferAccepted(**common, version_no=version_no or 0))
    elif to == "declined":
        emit(db, OfferDeclined(**common, version_no=version_no or 0))
    elif to == "expired":
        emit(db, OfferExpired(**common))
    await _timeline(ctx, db, offer, from_status, to)
    return offer


async def _timeline(ctx: RequestContext, db: AsyncSession, offer: Offer, from_status: str, to: str) -> None:
    """System entries on the candidate and demand timelines (ids and codes only)."""
    try:
        from app.services import activity_service
    except ImportError:  # pragma: no cover - Epic 2 absent
        return
    body = f"{offer.human_id}: {from_status} -> {to}"
    data = {"offer_id": str(offer.id), "from": from_status, "to": to}
    for entity_type, entity_id in (("candidate", offer.candidate_id), ("demand", offer.demand_id)):
        try:
            async with db.begin_nested():
                await activity_service.add_system_entry(ctx, db, entity_type, entity_id, body, data)
        except Exception:
            logger.exception("Offer timeline entry failed ({}:{})", entity_type, entity_id)
