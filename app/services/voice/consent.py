# ruff: noqa: E501
"""Call and recording consent (Epic 58, story 58.3; FR-294, NFR-51).

* ``send_request`` -- the templated consent request (system email template ``voice_consent_request``, coordinator C-11)
  with a fresh Secure Link (purpose ``voice_consent``, subject ``voice_session``, ``consent_link_days`` expiry).
* The consent page (``view`` / ``decide``): the Candidate confirms where they are (prefilled from the profile) and gives
  or refuses ``voice_screening`` (the call) and ``call_recording`` (recording) **separately** (source
  ``voice_consent_link``, evidence ``{link_id}``). On submit the Recording Policy gate runs for the confirmed jurisdiction
  and its decision is snapshotted on the hub (``jurisdiction``, ``recording_rule``, ``recording_allowed``, consent ids).
  ``voice_screening`` refused -> ``cancelled`` + ``voice_consent_refused``; granted with ``mode=call`` -> the agent's
  ``consented`` run schedules the call; ``mode=upload`` -> ``planned``.
"""

import uuid
from datetime import timedelta
from html import escape
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.voice import VoiceSession
from app.services.voice import common
from app.services.voice.common import PURPOSE_CALL, PURPOSE_RECORDING

CALL_NOTICE_TITLE = "Screening call"
CALL_NOTICE_BODY = (
    "We would like to have a short screening call with you about this role. A recruiter asks a few questions about "
    "your experience, availability and expectations.\n\n"
    "**Who listens:** the recruiter on the call and the recruiting team for this role.\n\n"
    "**AI processing:** if the call is transcribed, an AI assistant reads the **text** of the transcript to note your "
    "answers for the recruiter. It never analyses your voice, tone, accent, emotions or personality, and a person "
    "reviews every answer and makes every decision.\n\n"
    "**Sub-processor:** the AI model provider is a sub-processor that may process the transcript outside India.\n\n"
    "**Retention:** call notes and transcripts are kept for up to 180 days.\n\n"
    "**Withdrawing:** you can withdraw at any time from the link in our message or by contacting us. The transcript "
    "and the answers taken from it are then deleted."
)
RECORDING_NOTICE_TITLE = "Recording the screening call"
RECORDING_NOTICE_BODY = (
    "With your separate agreement, the screening call may be recorded so the recruiter can check your answers.\n\n"
    "**What is recorded:** the audio of the call only.\n\n"
    "**Who listens:** the recruiting team for this role.\n\n"
    "**AI processing:** the recording is never analysed by AI. Only the text of the transcript is used.\n\n"
    "**Sub-processor:** the AI model provider is a sub-processor that may process that transcript text outside "
    "India; the audio recording itself is never sent to it.\n\n"
    "**Retention:** recordings are deleted after 90 days.\n\n"
    "**Withdrawing:** you can withdraw this agreement at any time; the recording is then deleted within 24 hours. "
    "Recording happens only where the law of your location allows it."
)


async def _link_days(db: AsyncSession) -> int:
    return int((await common.settings_row(db)).consent_link_days)


async def issue_link(db: AsyncSession, row: VoiceSession, created_by: uuid.UUID | None) -> tuple[str, Any]:
    await secure_links.revoke_for_subject(db, common.LINK_SUBJECT, row.id, common.LINK_PURPOSE)
    link, token = await secure_links.issue(
        db, tenant_id=row.tenant_id, purpose=common.LINK_PURPOSE, subject_type=common.LINK_SUBJECT, subject_id=row.id,
        ttl=timedelta(days=await _link_days(db)), max_uses=20, created_by=created_by,
        metadata={"candidate_id": str(row.candidate_id)},
    )  # fmt: skip
    row.consent_link_id = link.id
    await db.flush()
    return secure_links.link_url(token), link.expires_at


async def _demand_id(db: AsyncSession, row: VoiceSession) -> uuid.UUID | None:
    from app.models.pipeline import Application

    if row.application_id is None:
        return None
    app = await db.get(Application, row.application_id)
    return app.demand_id if app is not None else None


async def _render(db: AsyncSession, row: VoiceSession, url: str) -> tuple[str, str]:
    from app.models.comms import EmailTemplateVariant
    from app.services import comms_service, entity_refs
    from app.services.comms import merge_fields

    template = await comms_service.active_template(db, common.TEMPLATE_KEY)
    variant = (
        (await db.execute(select(EmailTemplateVariant).where(EmailTemplateVariant.template_id == template.id))).scalars().first()
        if template is not None
        else None
    )  # fmt: skip
    recipient = await entity_refs.addressee(db, "candidate", row.candidate_id)
    data: dict[str, Any] = await entity_refs.merge_data(
        db, row.tenant_id, {"candidate_id": row.candidate_id, "demand_id": await _demand_id(db, row), "link_url": url},
        sender_user_id=row.owner_user_id, recipient=recipient,
    )  # fmt: skip
    if variant is None:
        return (
            "A short screening call",
            f'<p>We would like a short screening call with you.</p><p><a href="{escape(url)}">Choose your options</a></p>',
        )
    subject, _ = merge_fields.render(variant.subject, data, escape=False)
    body, _ = merge_fields.render(variant.body_html, data, escape=True)
    return subject, body


async def send_request(
    ctx: RequestContext, db: AsyncSession, row: VoiceSession, *, channel: str = "email", origin: str = "agent"
) -> dict[str, Any]:
    """Issue the consent link and send the templated request. Email only in Phase 4 (SMS / WhatsApp need tenant
    DLT / WhatsApp templates: ``channel_not_available``)."""
    from app.core.settings import get_settings
    from app.services import comms_service

    if row.status not in ("consent_pending", "planned"):
        raise common.conflict(
            "voice_session_closed", "This voice session no longer needs consent", status=row.status
        )
    if channel != "email":
        raise common.invalid("channel_not_available", "Consent requests are sent by email", channel=channel)
    try:
        recipient = await comms_service.recipient_for(db, "candidate", row.candidate_id)
    except Exception as exc:
        raise common.invalid("candidate_no_email", "The candidate has no email address") from exc
    url, expires_at = await issue_link(db, row, row.owner_user_id)
    subject, body = await _render(db, row, url)
    if origin == "agent":
        owner = await _recruiter_name(db, row)
        body += f"<p><em>{escape(get_settings().agent_disclosure_line.format(owner_name=owner))}</em></p>"
    result = await comms_service.queue_email(
        ctx, db, origin=origin, to=[recipient], subject=subject, body_html=body,
        related=[("candidate", row.candidate_id)] + ([("application", row.application_id)] if row.application_id else []),
        refs={"candidate_id": row.candidate_id}, sender_user_id=common.user_id_of(ctx) if origin == "user" else None,
        reply_to_user_id=row.owner_user_id, raw=True,
    )  # fmt: skip
    audit.record("voice_session.consent_requested", "voice_session", row.id, session=db, channel=channel)
    return {
        "message_id": result.message_ids[0] if result.message_ids else None,
        "link_expires_at": expires_at,
    }


async def _recruiter_name(db: AsyncSession, row: VoiceSession) -> str:
    from app.services import entity_refs

    info = await entity_refs.user(db, row.owner_user_id)
    return info.name if info is not None and info.name else "your recruiter"


# ================================================================ consent page
async def _link(token: str) -> secure_links.ResolvedLink:
    link = await secure_links.resolve(token, (common.LINK_PURPOSE,))
    if link.subject_type != common.LINK_SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    return link


async def _profile_location(db: AsyncSession, candidate_id: uuid.UUID) -> tuple[str | None, str | None]:
    from app.models.candidates import Candidate
    from app.services.recording_policy.gate import normalize_country

    cand = await db.get(Candidate, candidate_id)
    loc = getattr(cand, "location", None) if cand is not None else None
    if not isinstance(loc, dict):
        return None, None
    return normalize_country(loc.get("country")), (loc.get("state") or None)


async def _view(db: AsyncSession, row: VoiceSession, expires_at: Any = None) -> dict[str, Any]:
    from app.models.demands import Demand
    from app.services import entity_refs, privacy

    tenant = await entity_refs.tenant(db, row.tenant_id)
    who = await entity_refs.addressee(db, "candidate", row.candidate_id)
    notices = []
    for purpose, title, body in (
        (PURPOSE_CALL, CALL_NOTICE_TITLE, CALL_NOTICE_BODY),
        (PURPOSE_RECORDING, RECORDING_NOTICE_TITLE, RECORDING_NOTICE_BODY),
    ):
        notice = await privacy.consent.active_notice(db, row.tenant_id, purpose)
        notices.append(
            {
                "purpose": purpose,
                "title": notice.title if notice else title,
                "body": notice.body_md if notice else body,
            }
        )
    country, region = (row.confirmed_country, row.confirmed_region)
    if country is None:
        country, region = await _profile_location(db, row.candidate_id)
    demand_id = await _demand_id(db, row)
    demand = await db.get(Demand, demand_id) if demand_id else None
    voice = await common.consent_state(db, row, PURPOSE_CALL)
    rec = await common.consent_state(db, row, PURPOSE_RECORDING)
    return {
        "tenant_name": tenant.name, "recruiter_name": await _recruiter_name(db, row),
        "candidate_first_name": entity_refs.first_name(who.full_name if who else None),
        "demand_title": demand.title if demand is not None else None, "notices": notices, "country": country,
        "region": region, "voice_screening": voice, "call_recording": rec,
        "answered": row.confirmed_country is not None, "expires_at": expires_at,
    }  # fmt: skip


async def view(token: str) -> dict[str, Any]:
    from app.core.database import tenant_session

    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        row = await common.session_row(db, link.subject_id)
        return await _view(db, row, link.expires_at)


async def _set(
    ctx: RequestContext, db: AsyncSession, row: VoiceSession, purpose: str, agree: bool, link_id: uuid.UUID
) -> uuid.UUID | None:
    from app.models.privacy import Consent
    from app.services import privacy

    active = await common.has_consent(db, row.candidate_id, purpose)
    if agree:
        if not active:
            await privacy.consent.grant_consent(
                ctx, db, row.candidate_id, purpose, source=common.CONSENT_SOURCE,
                evidence={"link_id": str(link_id), "voice_session_id": str(row.id)},
            )  # fmt: skip
        return await common.active_consent_id(db, row.candidate_id, purpose)
    if active:
        await privacy.consent.withdraw_consent(ctx, db, row.candidate_id, purpose, by_kind="candidate")
    else:
        db.add(
            Consent(
                id=uuid.uuid4(), tenant_id=row.tenant_id, candidate_id=row.candidate_id, purpose=purpose,
                status="withdrawn", source=common.CONSENT_SOURCE, withdrawn_at=common.now(), withdrawn_by_kind="candidate",
                evidence={"link_id": str(link_id), "voice_session_id": str(row.id), "refused": True},
            )
        )  # fmt: skip
        await db.flush()
    return None


async def snapshot_gate(db: AsyncSession, row: VoiceSession) -> Any:
    """Run the Recording Policy gate for the confirmed (else profile) jurisdiction and snapshot it on the hub."""
    from app.services.recording_policy import gate

    rctx = common.reader_ctx(row.tenant_id, row.owner_user_id)
    confirmed = (row.confirmed_country, row.confirmed_region) if row.confirmed_country else None
    decision = await gate.check(
        rctx, db, purpose=PURPOSE_RECORDING, candidate_id=row.candidate_id, confirmed_jurisdiction=confirmed
    )  # type: ignore[arg-type]
    row.jurisdiction = decision.jurisdiction
    row.recording_rule = decision.rule
    if not decision.recording_allowed and row.recording_file_id is not None:
        from app.services.voice import withdrawal

        await withdrawal.purge_recording(db, row, reason="gate_changed")
    row.recording_allowed = bool(decision.recording_allowed)
    await db.flush()
    return decision


async def record_decision(
    db: AsyncSession,
    row: VoiceSession,
    *,
    country: str,
    region: str | None,
    voice: bool,
    recording: bool,
    link_id: uuid.UUID,
) -> None:
    from app.services.recording_policy.gate import normalize_country, normalize_region
    from app.services.voice import sessions

    if row.status in ("reviewed", "cancelled", "failed"):
        raise common.conflict("voice_session_closed", "This screening call request is closed")
    code = normalize_country(country)
    if code is None:
        raise common.invalid(
            "country_required", "Tell us which country you are in", fields={"country": "Required"}
        )
    ctx = RequestContext(tenant_id=row.tenant_id, actor_kind="system")
    row.confirmed_country = code
    row.confirmed_region = normalize_region(region)
    row.voice_consent_id = await _set(ctx, db, row, PURPOSE_CALL, voice, link_id)
    row.recording_consent_id = await _set(ctx, db, row, PURPOSE_RECORDING, recording and voice, link_id)
    decision = await snapshot_gate(db, row)
    audit.record(
        "voice_session.consent_recorded", "voice_session", row.id, session=db, actor_type="system", actor_id=None,
        voice_screening=voice, call_recording=recording and voice, rule=decision.rule, recording_allowed=decision.recording_allowed,
    )  # fmt: skip
    if not voice:
        row.status = "cancelled"
        await db.flush()
        common.fan_out(row.tenant_id, "voice_consent_refused", {
            "event_id": f"voice_refused:{row.id}", "voice_session_id": str(row.id), "user_id": str(row.owner_user_id),
            "application_id": str(row.application_id) if row.application_id else None,
        })  # fmt: skip
        await sessions.cancel_active_run(db, row, reason="The candidate declined the screening call")
        await common.timeline(
            ctx, db, row, "The candidate declined the voice screening call", "consent_refused"
        )
        return
    await common.timeline(ctx, db, row, "The candidate agreed to the voice screening call", "consent_granted")
    if row.status == "consent_pending":
        row.status = "planned"
        await db.flush()
        if row.mode == "call":
            await sessions.after_consent(ctx, db, row)


async def decide(
    token: str, country: str, region: str | None, voice: bool, recording: bool
) -> dict[str, Any]:
    from app.core.database import tenant_session

    link = await _link(token)
    async with tenant_session(link.tenant_id) as db:
        row = await common.session_row(db, link.subject_id, for_update=True)
        await record_decision(
            db, row, country=country, region=region, voice=voice, recording=recording, link_id=link.id
        )
        try:
            await secure_links.consume(db, link.id)
        except Exception:
            logger.debug("Voice consent link {} use not counted", link.id)
        return await _view(db, row, link.expires_at)


__all__ = [
    "CALL_NOTICE_BODY",
    "CALL_NOTICE_TITLE",
    "RECORDING_NOTICE_BODY",
    "RECORDING_NOTICE_TITLE",
    "decide",
    "issue_link",
    "record_decision",
    "send_request",
    "snapshot_gate",
    "view",
]
