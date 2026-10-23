# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Click-to-call (Epic 76, story 76.2, spec A3): call options, ★ placement, hang-up, reads and Do-Not-Call.

``place_call`` (human only, ``voice:call``): resolve / create the Voice Session, take the tenant cost-cap advisory lock,
run the gate (``gates.check``; the first failure is a 409 with the refusal code, nothing is sent to the provider),
decide recording *now* (policy + ``call_recording`` consent + notice), insert ``voice_calls`` with a 256-bit answer
token (stored as a hash) and place the bridged call through the adapter (sandbox -> Fake). Provider errors end the
attempt ``failed`` with the Manual instructions; the call is never retried automatically (a retry could ring twice).
"""

import secrets
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.core.integrations.errors import ConnectorError, ManualActionRequired
from app.core.pagination import Page
from app.core.settings import get_settings
from app.events.telephony import VoiceCallPlaced
from app.models.telephony import OPEN_CALL_STATUSES, SttJob, VoiceCall
from app.models.voice import OPEN_STATUSES, VoiceSession
from app.schemas.telephony import CallOptionsOut, ConsentFlags, DoNotCallOut, PlaceCallIn, VoiceCallOut
from app.services.connectors.voice.bridge import OP_BRIDGE_CALL, OP_HANGUP, BridgedCallRequest
from app.services.telephony import common, gates, numbers, user_phones

MANUAL_INSTRUCTIONS = "Call outside Talent OS, then upload the recording and/or transcript"


# ------------------------------------------------------------------------------------------------- reads
async def _transcript_status(db: AsyncSession, call: VoiceCall) -> str:
    status = (
        await db.execute(
            select(SttJob.status)
            .where(SttJob.source_kind == "voice_call", SttJob.source_id == call.id)
            .order_by(SttJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return status or "none"


async def out(db: AsyncSession, call: VoiceCall, *, manual: str | None = None) -> VoiceCallOut:
    return VoiceCallOut(
        id=call.id, human_id=call.human_id, voice_session_id=call.voice_session_id, application_id=call.application_id,
        candidate_id=call.candidate_id, placed_by=call.placed_by, provider_key=call.provider_key, status=call.status,  # type: ignore[arg-type]
        refusal_code=call.refusal_code, failure_code=call.failure_code, recorded=call.recording_requested,
        recording_unavailable_reason=call.recording_unavailable_reason, recording_status=call.recording_fetch_status,  # type: ignore[arg-type]
        transcript_status=await _transcript_status(db, call),  # type: ignore[arg-type]
        provider_delete_status=call.provider_delete_status, requested_at=call.requested_at, answered_at=call.answered_at,
        ended_at=call.ended_at, duration_s=call.duration_s, billable_minutes=call.billable_minutes,
        cost_estimate_minor=call.cost_estimate_minor, manual_instructions=manual,
    )  # fmt: skip


async def _session_for_read(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> VoiceSession:
    from app.services import voice_service

    await voice_service.get(ctx, db, session_id)  # 404 unless the caller can read the Application
    row = await db.get(VoiceSession, session_id)
    if row is None:
        raise common.not_found("Voice session")
    return row


async def _call(ctx: RequestContext, db: AsyncSession, call_id: uuid.UUID, *, for_update: bool = False) -> VoiceCall:
    stmt = select(VoiceCall).where(VoiceCall.id == call_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise common.not_found()
    try:
        await _session_for_read(ctx, db, row.voice_session_id)
    except Exception as exc:
        raise common.not_found() from exc
    return row


async def get_call(ctx: RequestContext, db: AsyncSession, call_id: uuid.UUID) -> VoiceCallOut:
    return await out(db, await _call(ctx, db, call_id))


async def list_calls(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    voice_session_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    placed_by: uuid.UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> Page[VoiceCallOut]:
    stmt = select(VoiceCall)
    if voice_session_id is not None:
        await _session_for_read(ctx, db, voice_session_id)
        stmt = stmt.where(VoiceCall.voice_session_id == voice_session_id)
    if candidate_id is not None:
        stmt = stmt.where(VoiceCall.candidate_id == candidate_id)
    if placed_by is not None:
        stmt = stmt.where(VoiceCall.placed_by == placed_by)
    if status is not None:
        stmt = stmt.where(VoiceCall.status == status)
    rows = list((await db.execute(stmt.order_by(VoiceCall.requested_at.desc()).limit(500))).scalars())
    readable: list[VoiceCall] = []
    seen: dict[uuid.UUID, bool] = {}
    for r in rows:  # row-level visibility follows the Application (P4 rule)
        if r.voice_session_id not in seen:
            try:
                await _session_for_read(ctx, db, r.voice_session_id)
                seen[r.voice_session_id] = True
            except Exception:
                seen[r.voice_session_id] = False
        if seen[r.voice_session_id]:
            readable.append(r)
    lo = (page - 1) * page_size
    items = [await out(db, r) for r in readable[lo : lo + page_size]]
    return Page[VoiceCallOut](items=items, total=len(readable), page=page, page_size=page_size)


# ------------------------------------------------------------------------------------------------- session
async def _open_session(db: AsyncSession, application_id: uuid.UUID) -> VoiceSession | None:
    return (
        await db.execute(
            select(VoiceSession).where(VoiceSession.application_id == application_id, VoiceSession.status.in_(OPEN_STATUSES))
        )
    ).scalar_one_or_none()


async def _latest_application(db: AsyncSession, candidate_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.pipeline import Application

    return (
        await db.execute(
            select(Application.id)
            .where(Application.candidate_id == candidate_id, Application.archived_at.is_(None))
            .order_by(Application.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def resolve_session(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    voice_session_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    create: bool = False,
) -> VoiceSession | None:
    """The open Voice Session for the subject; with ``create`` a new one is requested (``voice_service.request``) when
    ``voice_screening`` consent is on record (else None -> ``voice_consent_required``)."""
    from app.services import voice_service
    from app.services.voice.common import PURPOSE_CALL, has_consent

    if voice_session_id is not None:
        return await _session_for_read(ctx, db, voice_session_id)
    if application_id is None and candidate_id is not None:
        application_id = await _latest_application(db, candidate_id)
        if application_id is None:
            raise common.not_found("Application")
    assert application_id is not None
    await voice_service.list_for_application(ctx, db, application_id)  # 404 unless readable
    row = await _open_session(db, application_id)
    if row is not None or not create:
        return row
    from app.models.pipeline import Application

    app = await db.get(Application, application_id)
    if app is None or not await has_consent(db, app.candidate_id, PURPOSE_CALL):
        return None
    created = await voice_service.request(ctx, db, application_id, mode="upload", provider_key="manual")
    return await db.get(VoiceSession, created.id)


# ------------------------------------------------------------------------------------------------- options
async def call_options(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    application_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    voice_session_id: uuid.UUID | None = None,
) -> CallOptionsOut:
    from app.services.telephony import costs
    from app.services.voice.common import PURPOSE_CALL, PURPOSE_RECORDING, has_consent

    user_id = gates.require_caller(ctx)
    session = await resolve_session(
        ctx, db, voice_session_id=voice_session_id, application_id=application_id, candidate_id=candidate_id
    )
    if session is not None:
        cand = session.candidate_id
    elif candidate_id is not None:
        cand = candidate_id
    else:
        from app.models.pipeline import Application

        app = await db.get(Application, application_id)
        cand = app.candidate_id if app is not None else None
    phone_row = await user_phones.verified_row(db, user_id)
    consent = ConsentFlags(
        voice_screening=bool(cand) and await has_consent(db, cand, PURPOSE_CALL),  # type: ignore[arg-type]
        call_recording=bool(cand) and await has_consent(db, cand, PURPOSE_RECORDING),  # type: ignore[arg-type]
    )
    base: dict[str, Any] = {
        "consent": consent, "phone_verified": phone_row is not None,
        "phone_last4": phone_row.phone_last4 if phone_row else None,
        "voice_session_id": session.id if session else None,
    }  # fmt: skip
    if session is None:
        if not consent.voice_screening:
            return CallOptionsOut(can_call=False, refusal_code="voice_consent_required", refusal_message=gates.MESSAGES["voice_consent_required"], **base)
        session = VoiceSession(candidate_id=cand, tenant_id=common.tenant_of(db), owner_user_id=user_id)  # transient probe
    res = await gates.check(ctx, db, session)
    s = res.settings
    today_pct = costs.pct(res.today_minor or await costs.today(db), s.daily_cost_cap_minor) if s else 0
    if res.config is not None:
        base["provider_key"] = res.config.provider_key
    if res.number is not None:
        base["caller_id_label"] = numbers.caller_id_label(res.number)
    if not res.ok:
        nxt = res.details.get("next_window_at")
        return CallOptionsOut(
            can_call=False, refusal_code=res.code, refusal_message=res.message, next_window_at=nxt,  # type: ignore[arg-type]
            today_cost_pct=today_pct, **base,
        )  # fmt: skip
    can_notice = await _can_notice(ctx, db, res)
    record, reason = await gates.recording_decision(ctx, db, session, can_notice=can_notice)
    return CallOptionsOut(
        can_call=True, recording_will_be="recorded" if record else "not_recorded", recording_reason=reason,
        today_cost_pct=today_pct, **base,
    )  # fmt: skip


async def _adapter(ctx: RequestContext, db: AsyncSession, res: gates.GateResult) -> Any:
    from app.services.connectors.registry import build_for

    return await build_for(ctx, db, common.CATEGORY, res.config)


async def _can_notice(ctx: RequestContext, db: AsyncSession, res: gates.GateResult) -> bool:  # ruff: ignore[unused-function-argument]
    if res.config is None:
        return False
    if res.config.provider_key != "exotel":
        return True
    return bool((res.config.settings or {}).get("recording_flow_app_id"))


# ------------------------------------------------------------------------------------------------- place
async def place_call(ctx: RequestContext, db: AsyncSession, data: PlaceCallIn) -> VoiceCallOut:
    from app.core.sequences import next_human_id
    from app.services import voice_service
    from app.services.connectors.registry import webhook_secret
    from app.services.telephony import costs

    user_id = gates.require_caller(ctx)  # ★ human only
    session = await resolve_session(
        ctx, db, voice_session_id=data.voice_session_id, application_id=data.application_id,
        candidate_id=data.candidate_id, create=True,
    )  # fmt: skip
    if session is None:
        raise common.conflict("voice_consent_required", gates.MESSAGES["voice_consent_required"])
    if session.status not in OPEN_STATUSES:
        raise common.conflict("voice_session_closed", "This voice screen is closed", status=session.status)
    await costs.lock(db)  # serialise the cost-cap check per tenant (advisory, transaction scoped)
    res = await gates.check(ctx, db, session, phone_choice=data.phone_choice)
    if not res.ok:
        raise common.conflict(res.code or "refused", res.message or "The call can't be placed", **res.details)
    assert res.config is not None and res.number is not None and res.settings is not None and res.candidate_phone
    s = res.settings
    adapter = await _adapter(ctx, db, res)
    record, reason = await gates.recording_decision(ctx, db, session, can_notice=await _can_notice(ctx, db, res))
    opened = await user_phones.verified_phone(db, user_id)
    if opened is None:
        raise common.conflict("call_phone_not_verified", gates.MESSAGES["call_phone_not_verified"])
    _, agent_phone = opened
    token = secrets.token_urlsafe(32)
    rate = res.number.rate_per_minute_minor
    call = VoiceCall(
        id=uuid.uuid4(), tenant_id=session.tenant_id, human_id=await next_human_id(db, "voice_call", "CALL", width=6),
        voice_session_id=session.id, application_id=session.application_id, candidate_id=session.candidate_id,
        legal_entity_id=res.number.legal_entity_id, placed_by=user_id, installation_id=res.config.installation_id,
        config_id=res.config.id, number_id=res.number.id, provider_key=res.config.provider_key, status="requested",
        recording_requested=record, recording_unavailable_reason=reason, dual_channel=record,
        requested_at=common.now(), rate_per_minute_minor=rate, reserved_minor=s.max_call_minutes * rate,
        callback_token_hash=common.token_hash(token), recording_fetch_status="none", provider_delete_status="not_needed",
        token_expires_at=common.now() + timedelta(seconds=get_settings().telephony_answer_token_ttl_seconds),
    )  # fmt: skip
    db.add(call)
    await db.flush()
    base = get_settings().p5_public_webhook_base_url()
    hook = f"{base}/api/v1/hooks/voice/{res.config.id}"
    status_url = hook
    if res.config.provider_key == "exotel":
        path_token = await webhook_secret(ctx, db, res.config) or ""
        status_url = f"{hook}/exotel/{path_token}"
    answer_url = f"{hook}/answer/{token}" if res.config.provider_key in ("twilio", "plivo") else None
    tenant_name = await _tenant_name(db)
    notice = s.recording_notice_text.replace("{tenant}", tenant_name) if record else None
    req = BridgedCallRequest(
        voice_call_id=call.id, agent_e164=agent_phone, candidate_e164=res.candidate_phone,
        caller_id_e164=res.number.number_e164, record=record, dual_channel=record, notice_text=notice,
        status_callback_url=status_url, answer_url=answer_url, max_duration_s=s.max_call_minutes * 60,
    )  # fmt: skip
    manual: str | None = None
    try:
        result = await adapter.handle(OP_BRIDGE_CALL, req.to_payload())
        ref = str(result.get("external_ref") or "")
        if result.get("status") == "failed" or not ref:
            call.status = "failed"
            call.failure_code = str(result.get("failure_code") or "provider_failed")[:40]
        else:
            call.external_ref = ref
            call.status = "dialing_recruiter"
    except ManualActionRequired as exc:
        call.status = "failed"
        call.failure_code = "manual_required"
        manual = exc.instructions or MANUAL_INSTRUCTIONS
    except ConnectorError as exc:
        logger.warning("Telephony call {} not placed: {}", call.id, exc.code)
        call.status = "failed"
        call.failure_code = str(exc.code)[:40]
        manual = MANUAL_INSTRUCTIONS
    except Exception as exc:
        logger.warning("Telephony call {} not placed: {}", call.id, type(exc).__name__)
        call.status = "failed"
        call.failure_code = "provider_error"
        manual = MANUAL_INSTRUCTIONS
    if call.status == "failed":
        call.reserved_minor = 0
        call.cost_estimate_minor = 0
        call.ended_at = common.now()
        common.fan_out(call.tenant_id, "telephony_call_failed", {
            "event_id": f"telephony_call_failed:{call.id}", "user_id": str(user_id), "call_id": str(call.id),
            "application_id": str(call.application_id) if call.application_id else None,
        })  # fmt: skip
    else:
        await voice_service.on_provider_event(ctx, db, session.id, "call_placed", provider_key=call.provider_key)
        emit(db, VoiceCallPlaced(
            tenant_id=call.tenant_id, call_id=call.id, voice_session_id=session.id, candidate_id=call.candidate_id,
            placed_by=user_id, provider_key=call.provider_key, recorded=record,
        ))  # fmt: skip
    await db.flush()
    audit.record(
        "telephony.call_placed", "voice_call", call.id, session=db, status=call.status, provider_key=call.provider_key,
        recorded=record, recording_unavailable_reason=reason, failure_code=call.failure_code,
    )  # fmt: skip
    await costs.check_alerts(db, s)
    return await out(db, call, manual=manual)


async def _tenant_name(db: AsyncSession) -> str:
    from app.models.identity import Tenant

    name = (await db.execute(select(Tenant.name).where(Tenant.id == common.tenant_of(db)))).scalar_one_or_none()
    return name or "the recruiter"


async def hangup(ctx: RequestContext, db: AsyncSession, call_id: uuid.UUID) -> VoiceCallOut:
    gates.require_caller(ctx)
    call = await _call(ctx, db, call_id, for_update=True)
    if call.status not in OPEN_CALL_STATUSES:
        return await out(db, call)
    if call.placed_by != common.user_id_of(ctx):
        from app.core.permissions import Principal, Scope

        p = ctx.principal
        scope = p.permissions.get(common.PERM_CALL) if isinstance(p, Principal) else None
        if scope not in (Scope.TEAM, Scope.BU, Scope.TENANT):
            raise common.not_found()
    if call.config_id is not None and call.external_ref:
        from app.models.connectors import ProviderConnectorConfig
        from app.services.connectors.registry import build_for

        cfg = await db.get(ProviderConnectorConfig, call.config_id)
        try:
            adapter = await build_for(ctx, db, common.CATEGORY, cfg)
            await adapter.handle(OP_HANGUP, {"external_ref": call.external_ref})
        except Exception as exc:
            logger.info("Hang-up of {} not confirmed: {}", call.id, type(exc).__name__)
    audit.record("telephony.call_hangup", "voice_call", call.id, session=db)
    return await out(db, call)


async def do_not_call(ctx: RequestContext, db: AsyncSession, call_id: uuid.UUID) -> DoNotCallOut:
    """Record a ``voice`` opt-out for the Candidate's phones (Do-Not-Call)."""
    from app.services.messaging.opt_outs import record

    call = await _call(ctx, db, call_id)
    if call.candidate_id is None:
        return DoNotCallOut(recorded=False)
    phones = await gates.candidate_phones(db, call.candidate_id)
    for phone in phones:
        await record(
            db, "voice", phone, "user", recipient_kind="candidate", recipient_id=call.candidate_id,
            reason="do_not_call", created_by=common.user_id_of(ctx),
        )  # fmt: skip
    audit.record("telephony.do_not_call", "voice_call", call.id, session=db)
    return DoNotCallOut(recorded=bool(phones))
