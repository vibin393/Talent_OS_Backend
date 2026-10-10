"""Dispatch of the public secure-link pages (``/api/v1/public/links/{token}``) by link purpose.

* ``consent_request`` -- Epic 12's privacy consent page (plus the client name for ``client_submission``);
* ``ai_screening`` / ``client_feedback`` -- Epic 6 pipeline pages (``services.pipeline.public``);
* ``interview_scorecard`` -- Epic 7 external interviewer scorecard (``interviews_service``);
* ``offer_response`` -- Epic 8 candidate offer page (``offers_service.response``: view / letter / viewed /
  accept / decline).

Every GET only resolves and renders (no state change); POSTs are one-shot actions.
"""

import uuid
from typing import Any

from app.core import secure_links
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.schemas.pipeline import PublicActionOut, PublicLinkView
from app.schemas.privacy import PublicConsentOut

CONSENT_REQUEST = "consent_request"
PIPELINE_PURPOSES = ("ai_screening", "client_feedback")
OFFER_RESPONSE = "offer_response"  # Epic 8
INTERVIEW_SCORECARD = "interview_scorecard"  # Epic 7
ESIGN = "esign"  # Epic 22: signing ceremony (``/public/links/{token}/esign/*``)
CHANNEL_OPT_OUT = "channel_opt_out"  # Epic 33: SMS / WhatsApp opt-out link
JOINING_CONFIRMATION = "joining_confirmation"  # Epic 19: pre-joining touchpoint (``/public/links/{token}/joining``)
VENDOR_DATA_NOTICE = "vendor_data_notice"  # Epic 17: DPDP notice to vendor-sourced candidates (consent via /consent)
BGV_PROVIDER_CHECK = "bgv_provider_check"  # Epic 20: provider page (``/public/links/{token}/bgv-check``)
REFERENCE_CHECK = "reference_check"  # Epic 20: referee questionnaire (``/public/links/{token}/reference``)
EXTENSION_CONFIRMATION = "extension_confirmation"  # Epic 31: client confirms an extension (``/public/links/{token}/extension``)
OFFBOARDING_CONFIRMATION = "offboarding_confirmation"  # Epic 31: client confirms the last working day (``/offboarding``)
CLIENT_APPROVAL = "client_approval"  # Epic 18: Client User approval page (``/public/links/{token}/client-approval``)
IMMIGRATION_UPLOAD = "immigration_upload"  # Epic 46: counsel / Candidate upload page (``/immigration-upload``)
REFERRAL_LINK = "referral_link"  # Epic 42: referee accept / decline or share-link submit (``/referral``)
ASSESSMENT_ATTEMPT = "assessment_attempt"  # Epic 40: candidate test-taking page (``/public/links/{token}/assessment``)
CAMPUS_REGISTRATION = "campus_registration"  # Epic 43: campus drive registration (page ``/campus/r/{token}``)
EVENT_CHECKIN = "event_checkin"  # Epic 43: event registration / personal check-in pass (page ``/campus/e/{token}``)
AGENT_CONVERSATION = "agent_conversation"  # Epic 57: Screening Agent chat (page loads ``/public/screening-chat/{token}``)
SLOT_PICK = "slot_pick"  # Epic 59: Scheduling Agent self-scheduling page (page loads ``/public/slot-pick/{token}``)
INTERVIEW_RECORDING_CONSENT = "interview_recording_consent"  # Epic 60: Candidate recording consent page
INTERVIEW_CAPTURE_ACK = "interview_capture_ack"  # Epic 60: external interviewer recording agreement
VOICE_CONSENT = "voice_consent"  # Epic 58: Candidate voice screening / call recording consent page
VIEWABLE = (
    CONSENT_REQUEST,
    *PIPELINE_PURPOSES,
    OFFER_RESPONSE,
    INTERVIEW_SCORECARD,
    ESIGN,
    CHANNEL_OPT_OUT,
    JOINING_CONFIRMATION,
    VENDOR_DATA_NOTICE,
    BGV_PROVIDER_CHECK,
    REFERENCE_CHECK,
    EXTENSION_CONFIRMATION,
    OFFBOARDING_CONFIRMATION,
    CLIENT_APPROVAL,
    IMMIGRATION_UPLOAD,
    REFERRAL_LINK,
    ASSESSMENT_ATTEMPT,
    CAMPUS_REGISTRATION,
    EVENT_CHECKIN,
    AGENT_CONVERSATION,
    SLOT_PICK,
    INTERVIEW_RECORDING_CONSENT,
    INTERVIEW_CAPTURE_ACK,
    VOICE_CONSENT,
)


async def view_link(token: str) -> PublicLinkView:
    from app.services import clients_service, privacy
    from app.services.pipeline import public as pipeline_public

    link = await secure_links.resolve(token, VIEWABLE)
    if link.purpose in PIPELINE_PURPOSES:
        return await pipeline_public.view(link)
    if link.purpose == OFFER_RESPONSE:
        return await _offer_view(token, link)
    if link.purpose == INTERVIEW_SCORECARD:
        return await _scorecard_view(link)
    if link.purpose == ESIGN:
        return await _esign_view(link)
    if link.purpose == CHANNEL_OPT_OUT:
        return await _channel_opt_out_view(token)
    if link.purpose == JOINING_CONFIRMATION:
        return await _joining_view(token)
    if link.purpose == VENDOR_DATA_NOTICE:
        return await _vendor_notice_view(link)
    if link.purpose == CLIENT_APPROVAL:
        # Epic 50 (FR-252): the Client may require portal sign-in instead of approval links (403).
        await _assert_client_approval_allowed(link)
    if link.purpose in (BGV_PROVIDER_CHECK, REFERENCE_CHECK, EXTENSION_CONFIRMATION, OFFBOARDING_CONFIRMATION, CLIENT_APPROVAL):
        return await _bgv_view(link)
    if link.purpose == IMMIGRATION_UPLOAD:
        return await _immigration_view(link)
    if link.purpose == REFERRAL_LINK:
        return await _referral_view(link)
    if link.purpose in (CAMPUS_REGISTRATION, EVENT_CHECKIN):
        return await _campus_view(link)
    if link.purpose == ASSESSMENT_ATTEMPT:
        return await _assessment_view(link)
    if link.purpose == AGENT_CONVERSATION:
        return await _assessment_view(link)  # frame only (Epic 57 chat page)
    if link.purpose == SLOT_PICK:
        return await _assessment_view(link)  # frame only (Epic 59 slot-pick page)
    if link.purpose in (INTERVIEW_RECORDING_CONSENT, INTERVIEW_CAPTURE_ACK):
        return await _assessment_view(link)  # frame only (Epic 60 recording consent / agreement pages)
    if link.purpose == VOICE_CONSENT:
        return await _assessment_view(link)  # frame only (Epic 58 page loads ``/public/voice-consent/{token}``)
    base = await privacy.consent.view_link(token)
    out = PublicLinkView(**base.model_dump())
    client_raw = link.meta.get("client_id")
    if client_raw:
        async with tenant_session(link.tenant_id) as db:
            out.client_name = await clients_service.client_name(db, uuid.UUID(str(client_raw)))
    return out


async def _assert_client_approval_allowed(link: secure_links.ResolvedLink) -> None:
    from app.services import sso_service

    raw = (link.meta or {}).get("addressee_id")
    if raw:
        async with tenant_session(link.tenant_id) as db:
            await sso_service.assert_approval_link_allowed(db, uuid.UUID(str(raw)))


async def _purpose(token: str) -> str:
    link = await secure_links.resolve(token, VIEWABLE)
    return link.purpose


async def respond_consent(
    token: str, granted: bool, notice_id: uuid.UUID | None, *, ip_hash: str | None, user_agent: str | None
) -> PublicConsentOut | PublicActionOut:
    from app.services import privacy
    from app.services.pipeline import public as pipeline_public

    purpose = await _purpose(token)
    if purpose == "ai_screening":
        return await pipeline_public.consent(
            token, granted, notice_id, ip_hash=ip_hash, user_agent=user_agent
        )
    if purpose == VENDOR_DATA_NOTICE:
        from app.services.vendors import notice as vendor_notice

        return PublicActionOut(status=await vendor_notice.respond(token, granted))
    if purpose != CONSENT_REQUEST:
        raise NotFound("Link not found", code="link_invalid")
    return await privacy.consent.respond_link(
        token, granted, notice_id, ip_hash=ip_hash, user_agent=user_agent
    )


async def submit_answers(token: str, answers: dict[str, Any]) -> PublicActionOut:
    from app.services.pipeline import public as pipeline_public

    return await pipeline_public.submit(token, answers)


async def client_feedback(token: str, decision: str, comment: str | None) -> PublicActionOut:
    from app.services.pipeline import public as pipeline_public

    return await pipeline_public.feedback(token, decision, comment)


# ================================================================ Epic 8: offer response links
async def _offer_view(token: str, link: secure_links.ResolvedLink) -> PublicLinkView:
    from app.services import offers_service

    view = await offers_service.response.link_view(token)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=view.tenant_name,
        expires_at=link.expires_at,
        status=view.status,
        job_title=view.title,
        offer=view,
    )


async def offer_letter(token: str) -> tuple[bytes, str]:
    from app.services import offers_service

    return await offers_service.response.link_letter(token)


async def offer_viewed(token: str, ip: str | None, user_agent: str | None) -> Any:
    from app.services import offers_service

    return await offers_service.response.link_viewed(token, ip, user_agent)


async def offer_accept(
    token: str,
    *,
    typed_name: str,
    version_no: int,
    agreed: bool,
    ip: str | None,
    user_agent: str | None,
    otp: str | None = None,
) -> Any:
    from app.services import offers_service

    return await offers_service.response.link_accept(
        token, typed_name=typed_name, version_no=version_no, agreed=agreed, ip=ip, user_agent=user_agent, otp=otp
    )


async def offer_decline(
    token: str, *, reason: str | None, version_no: int, ip: str | None, user_agent: str | None
) -> Any:
    from app.services import offers_service

    return await offers_service.response.link_decline(
        token, reason=reason, version_no=version_no, ip=ip, user_agent=user_agent
    )


# ================================================================ Epic 7: external interviewer scorecards
async def _scorecard_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    from app.services import identity_bridge, interviews_service

    view = await interviews_service.public_scorecard_view(link)
    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant.name if tenant is not None else None,
        expires_at=link.expires_at,
        status=view.state,
        job_title=view.demand_title,
        scorecard=view,
    )


async def submit_scorecard(token: str, data: Any) -> PublicActionOut:
    """One-shot: validates, then consumes the link (reuse -> 410 ``link_used``)."""
    from app.services import interviews_service

    link = await secure_links.resolve(token, (INTERVIEW_SCORECARD,))
    return PublicActionOut(status=await interviews_service.public_scorecard_submit(link, data))


# ================================================================ Epic 22: eSignature links
async def _esign_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purpose ``esign``: only the page frame; the ceremony loads ``/public/links/{token}/esign``."""
    from app.services.documents import public as esign_public

    status, title, tenant_name = await esign_public.link_summary(link)
    return PublicLinkView(
        purpose=link.purpose, tenant_name=tenant_name, expires_at=link.expires_at, status=status, job_title=title
    )


# ================================================================ Epic 33: SMS / WhatsApp opt-out links
async def _channel_opt_out_view(token: str) -> PublicLinkView:
    from app.services import identity_bridge
    from app.services.messaging import portal as messaging_portal

    link, channel, opted_out = await messaging_portal.link_view(token)
    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant.name if tenant is not None else None,
        expires_at=link.expires_at,
        status="opted_out" if opted_out else "active",
        channel=channel,
    )


async def channel_opt_out(token: str) -> PublicActionOut:
    """POST: opt the number out of the link's channel (one use of the link)."""
    from app.services.messaging import portal as messaging_portal

    result = await messaging_portal.link_opt_out(token)
    return PublicActionOut(status=result.status)


# ================================================================ Epic 19: joining confirmation links
async def _joining_view(token: str) -> PublicLinkView:
    """Purpose ``joining_confirmation``: tenant, client display name, role and start date. Never mutates."""
    from app.services.placements import prejoining

    link, info, tenant_name = await prejoining.link_view(token)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant_name,
        expires_at=link.expires_at,
        status="responded" if info.responded else "open",
        job_title=info.role_title,
        client_name=info.client_name,
        candidate_first_name=info.candidate_first_name,
        joining=info,
    )


# ================================================================ Epic 17: vendor-sourced candidate notice
async def _vendor_notice_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purpose ``vendor_data_notice``: the active ``data_processing`` notice, the role and the current choice."""
    from app.schemas.privacy import NoticeOut
    from app.services.vendors import notice as vendor_notice

    view = await vendor_notice.view(link)
    notice = view["notice"]
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=view["tenant_name"],  # type: ignore[arg-type]
        expires_at=link.expires_at,
        notice=NoticeOut.model_validate(notice) if notice is not None else None,
        status=view["status"],  # type: ignore[arg-type]
        job_title=view["job_title"],  # type: ignore[arg-type]
        candidate_first_name=view["candidate_first_name"],  # type: ignore[arg-type]
        consent_required=True,
    )


# ================================================================ Epic 20: BGV provider / referee links
async def _bgv_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purposes ``bgv_provider_check`` / ``reference_check``: the page frame only (tenant, status); the page
    loads ``/public/links/{token}/bgv-check`` or ``/reference``. Never mutates."""
    from app.services import identity_bridge

    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant.name if tenant is not None else None,
        expires_at=link.expires_at,
        status="open",
    )


# ================================================================ Epic 46: immigration upload links
async def _immigration_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purpose ``immigration_upload``: the page frame only (tenant, status); the page loads
    ``/public/links/{token}/immigration-upload`` (case reference, items, notice). Never mutates."""
    from app.services.immigration import public as immigration_public

    status, tenant_name = await immigration_public.link_summary(link)
    return PublicLinkView(
        purpose=link.purpose, tenant_name=tenant_name, expires_at=link.expires_at, status=status, consent_required=True
    )


# ================================================================ Epic 42: referral links
async def _referral_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purpose ``referral_link``: the page frame only (tenant, status, role); the page loads
    ``/public/links/{token}/referral`` (notice, accept / decline or the share-link form). Never mutates."""
    from app.services.referrals import acceptance as referral_public

    status, tenant_name, title = await referral_public.link_summary(link)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant_name,
        expires_at=link.expires_at,
        status=status,
        job_title=title,
        consent_required=True,
    )


# ================================================================ Epic 40: assessment attempt links
async def _assessment_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purpose ``assessment_attempt``: the page frame only (tenant, status); the page loads
    ``/public/links/{token}/assessment`` (pre-start, notice, attempt). Never mutates; the link is multi-use."""
    from app.services import identity_bridge

    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant.name if tenant is not None else None,
        expires_at=link.expires_at,
        status="open",
        consent_required=True,
    )


# ================================================================ Epic 43: campus registration / event check-in links
async def _campus_view(link: secure_links.ResolvedLink) -> PublicLinkView:
    """Purposes ``campus_registration`` / ``event_checkin``: the page frame only (tenant, status); the frontend
    redirects to ``/campus/r/{token}`` or ``/campus/e/{token}``, which load ``/public/campus/*``. Never mutates."""
    from app.services import identity_bridge

    async with tenant_session(link.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
    return PublicLinkView(
        purpose=link.purpose,
        tenant_name=tenant.name if tenant is not None else None,
        expires_at=link.expires_at,
        status="open",
        consent_required=True,
    )
