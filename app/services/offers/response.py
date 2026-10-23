"""Candidate response to a released offer (story 8.2, FR-45): view, typed-name e-acceptance, decline.

``CandidateResponder(db, candidate_id, channel, ip, user_agent)`` is shared by the Candidate Portal API
(``channel="portal"``) and the secure-link fallback (``channel="secure_link"``):

* ``view`` returns the candidate view model and, on the first call, moves ``released -> viewed`` (actor
  ``candidate``). GET handlers never call it -- the page POSTs ``viewed`` from JavaScript so mail-scanner
  prefetches record nothing.
* ``accept`` locks the offer, checks status / expiry / the quoted released version / ``agreed`` / the typed name
  (NFKC, casefold, collapsed whitespace), writes ``offer_responses`` (IP, user agent, UTC time, letter SHA-256),
  transitions to ``accepted``, consumes the link and lets ``finalize_accepted_offer`` build the signed PDF.
* ``decline`` records the answer (optional reason) and transitions to ``declined``.
* Epic 22 (A9): the e-acceptance evidence lives on the signature tables. ``accept`` delegates it to
  ``documents.esign.SignerSession.sign`` (one signer, the candidate, frozen on the released letter; an emailed
  code first when ``offer_settings.acceptance_requires_otp``); ``decline`` declines the request. The
  ``offer_responses`` row and the offer transitions are unchanged.

View models never carry bill rate, burden or margin.
"""

import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import Gone, NotFound, ValidationFailed
from app.models.offers import Offer, OfferResponse, OfferVersion
from app.schemas.offers import CandidateOfferResponse, CandidateOfferView, Money, OfferActionOut
from app.services.offers import status as status_machine
from app.services.offers.common import (
    LINK_PURPOSE,
    OPEN_FOR_RESPONSE,
    acting_as,
    candidate_facts,
    conflict,
    money,
    now,
    tenant_facts,
)

VISIBLE_STATUSES = ("released", "viewed", "accepted", "declined", "expired", "withdrawn")
_SPACES = re.compile(r"\s+")


# Epic 22: moved to ``documents/esign.py`` (re-exported here).
from app.services.documents.esign import (  # ruff: ignore[module-import-not-at-top-of-file]
    clean_ip,
    normalize_name,
)


async def released_version(db: AsyncSession, offer: Offer) -> OfferVersion:
    version_id = offer.released_version_id or offer.current_version_id
    version = await db.get(OfferVersion, version_id) if version_id else None
    if version is None:
        raise NotFound("Offer not found")
    return version


def is_visible(offer: Offer) -> bool:
    return offer.released_at is not None and offer.status in VISIBLE_STATUSES


async def candidate_view(db: AsyncSession, offer: Offer, *, letter_url: str | None) -> CandidateOfferView:
    version = await released_version(db, offer)
    tenant = await tenant_facts(db, offer.tenant_id)
    candidate = await candidate_facts(db, offer.candidate_id)
    response_row = (
        await db.execute(
            select(OfferResponse)
            .where(OfferResponse.offer_id == offer.id)
            .order_by(OfferResponse.decided_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    pay = money(version.pay_rate, version.pay_currency)
    assert pay is not None
    bonus = money(version.bonus_amount, version.bonus_currency)
    can_respond = (
        offer.status in OPEN_FOR_RESPONSE and offer.expires_at is not None and now() < offer.expires_at
    )
    return CandidateOfferView(
        id=offer.id,
        reference=offer.human_id,
        status=offer.status,  # type: ignore[arg-type]
        version_no=version.version_no,
        tenant_name=tenant["name"],
        candidate_name=candidate.get("full_name"),
        title=version.title,
        employment_type=version.employment_type,  # type: ignore[arg-type]
        pay_rate=Money(**pay),
        pay_rate_unit=version.pay_rate_unit,  # type: ignore[arg-type]
        bonus=Money(**bonus) if bonus else None,
        benefits_summary=version.benefits_summary,
        start_date=version.start_date,
        end_date=version.end_date,
        location=version.location,
        work_mode=version.work_mode,  # type: ignore[arg-type]
        expiry_date=version.expiry_date,
        expires_at=offer.expires_at,
        released_at=offer.released_at,
        letter_available=version.letter_file_id is not None,
        letter_url=letter_url if version.letter_file_id is not None else None,
        can_respond=can_respond,
        response=CandidateOfferResponse(
            decision=response_row.decision,  # type: ignore[arg-type]
            version_no=response_row.version_no,
            typed_name=response_row.typed_name,
            decided_at=response_row.decided_at,
        )
        if response_row is not None
        else None,
    )


class CandidateResponder:
    """One candidate acting on their own offers through one channel."""

    def __init__(
        self,
        db: AsyncSession,
        candidate_id: uuid.UUID,
        channel: str,
        ip: str | None,
        user_agent: str | None,
        *,
        link_id: uuid.UUID | None = None,
    ) -> None:
        self.db = db
        self.candidate_id = candidate_id
        self.channel = channel
        self.ip = clean_ip(ip)
        self.user_agent = (user_agent or "")[:500] or None
        self.link_id = link_id

    @property
    def audit_via(self) -> str:
        """``candidate`` (portal session) or ``secure_link``. The audit ``actor_type`` is always ``candidate``
        (the audit log's actor types); the channel is recorded as ``via``."""
        return "candidate" if self.channel == "portal" else "secure_link"

    def _ctx(self, offer: Offer) -> RequestContext:
        return RequestContext(tenant_id=offer.tenant_id, ip=self.ip, user_agent=self.user_agent)

    async def load(self, offer_id: uuid.UUID, *, for_update: bool = False) -> Offer:
        stmt = select(Offer).where(Offer.id == offer_id, Offer.candidate_id == self.candidate_id)
        if for_update:
            stmt = stmt.with_for_update(of=Offer)
        offer = (await self.db.execute(stmt)).scalar_one_or_none()
        if offer is None or not is_visible(offer):
            raise NotFound("Offer not found")
        return offer

    def _audit(self, action: str, offer: Offer, **data: Any) -> None:
        audit.record(
            action,
            "offer",
            offer.id,
            session=self.db,
            actor_type="candidate",
            actor_id=self.candidate_id,
            channel=self.channel,
            via=self.audit_via,
            **data,
        )

    async def view(self, offer_id: uuid.UUID, *, letter_url: str | None = None) -> CandidateOfferView:
        offer = await self.load(offer_id, for_update=True)
        if offer.status == "released":
            with acting_as(self.db, "candidate", self.candidate_id):
                await status_machine.transition_offer(
                    self._ctx(offer), self.db, offer, "viewed", "candidate", actor_ref=self.candidate_id
                )
        self._audit("offer.viewed", offer, status=offer.status)
        await self.db.flush()
        return await candidate_view(self.db, offer, letter_url=letter_url)

    async def _open_for_answer(self, offer_id: uuid.UUID, version_no: int) -> tuple[Offer, OfferVersion]:
        offer = await self.load(offer_id, for_update=True)
        if offer.status not in OPEN_FOR_RESPONSE:
            raise conflict("offer_not_open", "This offer can no longer be answered", status=offer.status)
        if offer.expires_at is None or now() >= offer.expires_at:
            raise Gone("This offer has expired", code="offer_expired")
        version = await released_version(self.db, offer)
        if version_no != version.version_no:
            raise conflict(
                "offer_version_changed",
                "This offer was updated; reload to review the latest version",
                version_no=version.version_no,
            )
        return offer, version

    async def _write(
        self,
        offer: Offer,
        version: OfferVersion,
        decision: str,
        *,
        typed_name: str | None = None,
        reason: str | None = None,
    ) -> OfferResponse:
        if self.link_id is not None:
            await secure_links.consume(self.db, self.link_id)
        row = OfferResponse(
            id=uuid.uuid4(),
            tenant_id=offer.tenant_id,
            offer_id=offer.id,
            candidate_id=offer.candidate_id,
            version_id=version.id,
            version_no=version.version_no,
            decision=decision,
            typed_name=typed_name,
            decline_reason=reason,
            ip=self.ip,
            user_agent=self.user_agent,
            channel=self.channel,
            letter_sha256=version.letter_sha256,
            decided_at=now(),
        )
        with acting_as(self.db, "candidate", self.candidate_id):
            self.db.add(row)
            await self.db.flush()
        return row

    async def _esign_request(self, offer: Offer, version: OfferVersion) -> Any:
        """Epic 22: the open e-acceptance signature request of the released letter (created on demand)."""
        from app.services.documents import esign
        from app.services.offers import versions as offer_versions
        from app.services.offers.common import settings_row

        if version.letter_file_id is None:
            await offer_versions.ensure_letter(self._ctx(offer), self.db, offer, version)
        settings = await settings_row(self.db)
        req = await esign.ensure_offer_request(
            self.db, offer_id=offer.id, candidate_id=offer.candidate_id, offer_ref=offer.human_id,
            version_no=version.version_no, letter_file_id=version.letter_file_id, expires_at=offer.expires_at,
            require_otp=bool(settings.acceptance_requires_otp), current_request_id=offer.signature_request_id,
        )  # fmt: skip
        if offer.signature_request_id != req.id:
            offer.signature_request_id = req.id
            await self.db.flush()
        return req

    async def _signer_session(self, offer: Offer, version: OfferVersion) -> Any:
        from app.services.documents import esign

        req = await self._esign_request(offer, version)
        signer = await esign.offer_signer(self.db, req.id)
        return esign.SignerSession(
            self.db, signer.id, self.channel, self.ip, self.user_agent, actor_kind="candidate"
        )

    async def otp_required(self, offer_id: uuid.UUID) -> bool:
        from app.services.offers.common import settings_row

        await self.load(offer_id)
        return bool((await settings_row(self.db)).acceptance_requires_otp)

    async def request_otp(self, offer_id: uuid.UUID) -> tuple[Any, str]:
        """Epic 22: email a one-time code to the candidate before accepting (open offers only)."""
        offer = await self.load(offer_id, for_update=True)
        if offer.status not in OPEN_FOR_RESPONSE:
            raise conflict("offer_not_open", "This offer can no longer be answered", status=offer.status)
        if offer.expires_at is None or now() >= offer.expires_at:
            raise Gone("This offer has expired", code="offer_expired")
        version = await released_version(self.db, offer)
        session = await self._signer_session(offer, version)
        return await session.request_otp()  # type: ignore[no-any-return]

    async def accept(
        self, offer_id: uuid.UUID, typed_name: str, version_no: int, agreed: bool, otp: str | None = None
    ) -> CandidateOfferView:
        offer, version = await self._open_for_answer(offer_id, version_no)
        if not agreed:
            raise ValidationFailed(
                "Confirm that typing your name is your electronic signature", code="agreement_required"
            )
        candidate = await candidate_facts(self.db, offer.candidate_id)
        typed = _SPACES.sub(" ", unicodedata.normalize("NFKC", typed_name)).strip()
        if not typed or normalize_name(typed) != normalize_name(candidate.get("full_name")):
            raise ValidationFailed(
                "The typed name does not match the name on the offer", code="name_mismatch"
            )
        session = await self._signer_session(offer, version)
        with acting_as(self.db, "candidate", self.candidate_id):
            await session.sign(typed, otp, agreed=True)
        await self._write(offer, version, "accepted", typed_name=typed)
        with acting_as(self.db, "candidate", self.candidate_id):
            await status_machine.transition_offer(
                self._ctx(offer), self.db, offer, "accepted", "candidate", actor_ref=self.candidate_id
            )
        self._audit(
            "offer.accepted", offer, version_no=version.version_no, letter_sha256=version.letter_sha256
        )
        await self.db.flush()
        return await candidate_view(self.db, offer, letter_url=None)

    async def decline(self, offer_id: uuid.UUID, reason: str | None, version_no: int) -> CandidateOfferView:
        offer, version = await self._open_for_answer(offer_id, version_no)
        await self._write(offer, version, "declined", reason=(reason or "").strip() or None)
        if offer.signature_request_id is not None:
            from app.services.documents import esign

            with acting_as(self.db, "candidate", self.candidate_id):
                await esign.decline_offer_request(self.db, offer.signature_request_id)
        with acting_as(self.db, "candidate", self.candidate_id):
            await status_machine.transition_offer(
                self._ctx(offer),
                self.db,
                offer,
                "declined",
                "candidate",
                reason=None,
                actor_ref=self.candidate_id,
            )
        self._audit("offer.declined", offer, version_no=version.version_no)
        await self.db.flush()
        return await candidate_view(self.db, offer, letter_url=None)

    async def letter(self, offer_id: uuid.UUID) -> tuple[bytes, str]:
        from app.services import files_service

        offer = await self.load(offer_id)
        version = await released_version(self.db, offer)
        file_id = version.letter_file_id
        if offer.status == "accepted" and offer.accepted_file_id is not None:
            file_id = offer.accepted_file_id
        stored = await files_service.get_file(self.db, file_id) if file_id else None
        if stored is None or stored.deleted_at is not None:
            raise NotFound("The offer letter is not available", code="letter_not_ready")
        data = await files_service.read_file(stored)
        return data, f"{offer.human_id}-v{version.version_no}.pdf"


# ================================================================ Candidate Portal (Epic 9 wires the principal)
def _portal_letter_url(offer_id: uuid.UUID) -> str:
    return f"/api/v1/portal/offers/{offer_id}/letter"


async def portal_list(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> list[CandidateOfferView]:
    async with tenant_session(tenant_id) as db:
        rows = (
            await db.execute(
                select(Offer)
                .where(Offer.candidate_id == candidate_id, Offer.released_at.is_not(None))
                .order_by(Offer.released_at.desc())
            )
        ).scalars()
        return [
            await candidate_view(db, o, letter_url=_portal_letter_url(o.id))
            for o in list(rows)
            if is_visible(o)
        ]


async def portal_get(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID
) -> CandidateOfferView:
    async with tenant_session(tenant_id) as db:
        responder = CandidateResponder(db, candidate_id, "portal", None, None)
        offer = await responder.load(offer_id)
        return await candidate_view(db, offer, letter_url=_portal_letter_url(offer.id))


async def portal_viewed(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID, ip: str | None, user_agent: str | None
) -> CandidateOfferView:
    async with tenant_session(tenant_id) as db:
        responder = CandidateResponder(db, candidate_id, "portal", ip, user_agent)
        return await responder.view(offer_id, letter_url=_portal_letter_url(offer_id))


async def portal_letter(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID
) -> tuple[bytes, str]:
    async with tenant_session(tenant_id) as db:
        return await CandidateResponder(db, candidate_id, "portal", None, None).letter(offer_id)


async def portal_accept(
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    offer_id: uuid.UUID,
    *,
    typed_name: str,
    version_no: int,
    agreed: bool,
    ip: str | None,
    user_agent: str | None,
    otp: str | None = None,
) -> OfferActionOut:
    async with tenant_session(tenant_id) as db:
        view = await CandidateResponder(db, candidate_id, "portal", ip, user_agent).accept(
            offer_id, typed_name, version_no, agreed, otp
        )
    return OfferActionOut(status="accepted", offer=view)


async def portal_decline(
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    offer_id: uuid.UUID,
    *,
    reason: str | None,
    version_no: int,
    ip: str | None,
    user_agent: str | None,
) -> OfferActionOut:
    async with tenant_session(tenant_id) as db:
        view = await CandidateResponder(db, candidate_id, "portal", ip, user_agent).decline(
            offer_id, reason, version_no
        )
    return OfferActionOut(status="declined", offer=view)


# ================================================================ secure-link fallback (Epic 6 public router)
async def _link(token: str) -> secure_links.ResolvedLink:
    link = await secure_links.resolve(token, (LINK_PURPOSE,))
    if link.subject_type != "offer":
        raise NotFound("Link not found", code="link_invalid")
    return link


async def _link_candidate(db: AsyncSession, link: secure_links.ResolvedLink) -> uuid.UUID:
    offer = await db.get(Offer, link.subject_id)
    if offer is None:
        raise NotFound("Link not found", code="link_invalid")
    return offer.candidate_id


async def link_view(token: str) -> CandidateOfferView:
    """``GET /public/links/{token}`` for purpose ``offer_response``: renders only (records nothing)."""
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(db, await _link_candidate(db, link), "secure_link", None, None)
        offer = await responder.load(link.subject_id)
        return await candidate_view(db, offer, letter_url=None)


async def link_letter(token: str) -> tuple[bytes, str]:
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(db, await _link_candidate(db, link), "secure_link", None, None)
        return await responder.letter(link.subject_id)


async def link_viewed(token: str, ip: str | None, user_agent: str | None) -> CandidateOfferView:
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(db, await _link_candidate(db, link), "secure_link", ip, user_agent)
        return await responder.view(link.subject_id)


async def link_accept(
    token: str,
    *,
    typed_name: str,
    version_no: int,
    agreed: bool,
    ip: str | None,
    user_agent: str | None,
    otp: str | None = None,
) -> OfferActionOut:
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(
            db, await _link_candidate(db, link), "secure_link", ip, user_agent, link_id=link.id
        )
        view = await responder.accept(link.subject_id, typed_name, version_no, agreed, otp)
    return OfferActionOut(status="accepted", offer=view)


async def link_decline(
    token: str, *, reason: str | None, version_no: int, ip: str | None, user_agent: str | None
) -> OfferActionOut:
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(
            db, await _link_candidate(db, link), "secure_link", ip, user_agent, link_id=link.id
        )
        view = await responder.decline(link.subject_id, reason, version_no)
    return OfferActionOut(status="declined", offer=view)


# ================================================================ Epic 22: emailed acceptance code
async def portal_otp_required(tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID) -> bool:
    async with tenant_session(tenant_id) as db:
        return await CandidateResponder(db, candidate_id, "portal", None, None).otp_required(offer_id)


async def portal_request_otp(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID, ip: str | None, user_agent: str | None
) -> tuple[Any, str]:
    async with tenant_session(tenant_id) as db:
        return await CandidateResponder(db, candidate_id, "portal", ip, user_agent).request_otp(offer_id)


async def link_otp_required(token: str) -> bool:
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(db, await _link_candidate(db, link), "secure_link", None, None)
        return await responder.otp_required(link.subject_id)


async def link_request_otp(token: str, ip: str | None, user_agent: str | None) -> tuple[Any, str]:
    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        responder = CandidateResponder(db, await _link_candidate(db, link), "secure_link", ip, user_agent)
        return await responder.request_otp(link.subject_id)
