"""Offers service facade (Epic 8: offers, versions, approval, letters, release, candidate response, expiry).

Public API for routers and other domains (``from app.services import offers_service``):

* ``transition_offer(ctx, db, offer, to, actor, reason)`` -- the only writer of ``offers.status``;
  ``TRANSITIONS`` is the §5.3 table.
* ``create_offer`` / ``update_offer`` / ``submit_offer`` / ``release_offer`` / ``withdraw_offer`` and reads.
* ``CandidateResponder`` and the portal / secure-link handlers (``response``) -- used by the Candidate Portal
  API (``/api/v1/portal/offers``) and the public link router.
* ``ensure_default_templates`` (tenant provisioning), ``MERGE_FIELDS``.
* ``HUMAN_ONLY`` -- FR-65 probes (``offer.release``) that must refuse ``ai`` / ``workflow`` with 409.

Modules: ``status``, ``offers``, ``versions``, ``templates``, ``letter_pdf``, ``response``, ``notifications``,
``expiry``, ``handlers``, ``common``.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.offers import (
    common,
    expiry,
    handlers,
    letter_pdf,
    notifications,
    offers,
    response,
    status,
    templates,
    versions,
)
from app.services.offers.offers import (
    WITHDRAWAL_REASON_PRESETS,
    BulkOfferResult,
    create_bulk,
    create_offer,
    get_offer,
    get_settings,
    get_version,
    list_for_application,
    list_for_demand,
    list_offers,
    release,
    release_offer,
    submit_offer,
    update_offer,
    update_settings,
    withdraw_offer,
)
from app.services.offers.response import CandidateResponder, normalize_name
from app.services.offers.status import (
    TRANSITIONS,
    allowed_actors,
    check_transition,
    record_placement_dropped,
    transition_offer,
)
from app.services.offers.templates import MERGE_FIELDS, ensure_default_templates


async def _probe_release(actor: str) -> None:  # NOSONAR
    """FR-65 probe: releasing an approved offer as ``actor`` (409 for ai / workflow)."""
    check_transition("approved", "released", actor)


async def _probe_accept(actor: str) -> None:  # NOSONAR
    """FR-65 probe: only the candidate may accept."""
    check_transition("released", "accepted", actor)


HUMAN_ONLY: dict[str, Any] = {"offer.release": _probe_release, "offer.accept": _probe_accept}


# ---------------------------------------------------------------- Epic 9 convenience (candidate principal)
async def list_for_candidate(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> list[Any]:
    return await response.portal_list(tenant_id, candidate_id)


async def get_for_candidate(tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID) -> Any:
    return await response.portal_get(tenant_id, candidate_id, offer_id)


async def mark_viewed(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID, ip: str | None, user_agent: str | None
) -> Any:
    return await response.portal_viewed(tenant_id, candidate_id, offer_id, ip, user_agent)


def templates_preview(body_md: str) -> bytes:
    """PDF of a template rendered with the sample merge context (template editor preview; 422 on bad fields)."""
    markdown = templates.validate_template(body_md)
    sample = templates.MERGE_FIELDS
    return letter_pdf.render(
        markdown, tenant_name=sample["tenant.name"][1], offer_ref="OFR-PREVIEW", version_no=1
    )


async def merge_fields_for(db: AsyncSession, offer_id: uuid.UUID) -> dict[str, Any]:
    """Epic 10 email merge fields of an offer (candidate-safe: no bill rate, burden or margin)."""
    from app.models.offers import Offer

    offer = await db.get(Offer, offer_id)
    if offer is None:
        return {}
    version = await response.released_version(db, offer)
    return {
        "reference": offer.human_id,
        "status": offer.status,
        "title": version.title,
        "start_date": common.fmt_date(version.start_date),
        "expiry_date": common.fmt_date(version.expiry_date),
        "portal_url": notifications.portal_url(offer.id),
    }


_registered = False


def register() -> None:
    """Idempotent wiring: event handlers, approvals subject, file access hook."""
    global _registered  # process-wide wiring
    if _registered:
        return
    handlers.register()
    offers.register_approvals()
    from app.services import files_service

    files_service.register_access_checker("offer", offers.offer_access, plural="offers")
    _registered = True


async def offer_access(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, write: bool) -> bool:
    return await offers.offer_access(ctx, db, offer_id, write)


register()

__all__ = [
    "HUMAN_ONLY",
    "MERGE_FIELDS",
    "TRANSITIONS",
    "WITHDRAWAL_REASON_PRESETS",
    "BulkOfferResult",
    "CandidateResponder",
    "allowed_actors",
    "check_transition",
    "common",
    "create_bulk",
    "create_offer",
    "ensure_default_templates",
    "expiry",
    "get_for_candidate",
    "get_offer",
    "get_settings",
    "get_version",
    "handlers",
    "letter_pdf",
    "list_for_application",
    "list_for_candidate",
    "list_for_demand",
    "list_offers",
    "mark_viewed",
    "merge_fields_for",
    "normalize_name",
    "notifications",
    "offer_access",
    "offers",
    "record_placement_dropped",
    "register",
    "release",
    "release_offer",
    "response",
    "status",
    "submit_offer",
    "templates",
    "templates_preview",
    "transition_offer",
    "update_offer",
    "update_settings",
    "versions",
    "withdraw_offer",
]
