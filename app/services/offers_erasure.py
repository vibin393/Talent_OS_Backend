"""Offers-domain erasure handler (Epic 12 registry, FR-68): ``offers.documents`` (56).

Erasure keeps the offer facts (ids, statuses, rates, dates -- needed for demand counts and audit) and removes
the candidate's personal data: the rendered letter bodies, the stored letter / signed PDFs (deleted from
storage, rows kept as tombstones), the typed name, IP address, user agent and decline reason of responses,
and free-text withdrawal / approval comments.
"""

from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.files import StoredFile
from app.models.offers import Offer, OfferResponse, OfferVersion
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


class OffersHandler:
    name = "offers.documents"
    order = 56
    tables = ("offers", "offer_versions", "offer_responses")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        offers = list((await db.execute(select(Offer).where(Offer.candidate_id.in_(ids)))).scalars())
        offer_ids = [o.id for o in offers]
        file_ids = [o.accepted_file_id for o in offers if o.accepted_file_id]
        for offer in offers:
            offer.withdrawn_reason = None
            offer.status_comment = None
        versions = (
            list(
                (await db.execute(select(OfferVersion).where(OfferVersion.offer_id.in_(offer_ids)))).scalars()
            )
            if offer_ids
            else []
        )
        for v in versions:
            v.rendered_body = None
            if v.letter_file_id:
                file_ids.append(v.letter_file_id)
        responses = list(
            (await db.execute(select(OfferResponse).where(OfferResponse.candidate_id.in_(ids)))).scalars()
        )
        for r in responses:
            r.typed_name = ERASED if r.typed_name else None
            r.ip = None
            r.user_agent = None
            r.decline_reason = None
        deleted = 0
        if file_ids:
            backend = storage.get_storage()
            at = now()
            for file in (await db.execute(select(StoredFile).where(StoredFile.id.in_(file_ids)))).scalars():
                if file.deleted_at is None:
                    try:
                        await backend.delete(file.storage_key)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        logger.exception("Could not delete offer letter {} from storage", file.id)
                        raise
                    file.deleted_at = at
                    deleted += 1
                file.original_name = ERASED
        await db.flush()
        return {"offers": len(offers), "offer_responses": len(responses), "files_deleted": deleted}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        offers = list(
            (await db.execute(select(Offer).where(Offer.candidate_id == ctx.candidate_id))).scalars()
        )
        responses = (
            await db.execute(select(OfferResponse).where(OfferResponse.candidate_id == ctx.candidate_id))
        ).scalars()
        return {
            "offers": [
                {
                    "human_id": o.human_id,
                    "status": o.status,
                    "released_at": o.released_at,
                    "expires_at": o.expires_at,
                }
                for o in offers
            ],
            "offer_responses": [
                {
                    "decision": r.decision,
                    "version_no": r.version_no,
                    "typed_name": r.typed_name,
                    "decline_reason": r.decline_reason,
                    "ip": str(r.ip) if r.ip is not None else None,
                    "user_agent": r.user_agent,
                    "decided_at": r.decided_at,
                }
                for r in responses
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        left = await db.execute(
            select(OfferResponse.id).where(
                OfferResponse.candidate_id.in_(ids),
                or_(
                    OfferResponse.ip.is_not(None),
                    OfferResponse.user_agent.is_not(None),
                    OfferResponse.decline_reason.is_not(None),
                    OfferResponse.typed_name.not_in([ERASED]),
                ),
            )
        )
        findings += [f"offer response {rid} still holds personal data" for rid in left.scalars()]
        offer_ids = select(Offer.id).where(Offer.candidate_id.in_(ids))
        bodies = await db.execute(
            select(OfferVersion.id).where(
                OfferVersion.offer_id.in_(offer_ids), OfferVersion.rendered_body.is_not(None)
            )
        )
        findings += [f"offer version {vid} still holds a letter body" for vid in bodies.scalars()]
        file_ids = [
            f
            for f in (
                await db.execute(
                    select(OfferVersion.letter_file_id).where(OfferVersion.offer_id.in_(offer_ids))
                )
            ).scalars()
            if f
        ] + [
            f
            for f in (
                await db.execute(select(Offer.accepted_file_id).where(Offer.candidate_id.in_(ids)))
            ).scalars()
            if f
        ]
        if file_ids:
            live = await db.execute(
                select(StoredFile.id).where(StoredFile.id.in_(file_ids), StoredFile.deleted_at.is_(None))
            )
            findings += [f"offer letter {fid} not deleted" for fid in live.scalars()]
        return findings


register_erasure_handler(OffersHandler())
