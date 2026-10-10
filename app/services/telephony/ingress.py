# ruff: noqa: E501, D401
"""Call lifecycle from provider callbacks (Epic 76, stories 76.2 / 76.4, spec A3).

Callbacks arrive at the foundation ingress ``/api/v1/hooks/voice/{config_id}`` (manifest-driven verification,
``external_event_id`` dedup, ``held`` while the Ecosystem Kill Switch is on) and are processed by the ``voice`` webhook
handler; ``intercept`` (registered with P4 ``provider_jobs.register_event_interceptor``) handles payloads of
``voice_calls`` first and leaves the rest to P4.

* Exotel: **verify-by-fetch** -- the callback content is never applied; ``OP_CALL_STATUS`` is fetched and only the
  fetched status is applied. Exotel callbacks reach the ingress through ``relay_exotel`` (path token + IP allow-list).
* Out-of-order events never move a terminal status back; duplicates are dropped by the ingress.
* Terminal status: costs + metering, the ``call`` activity (no phone numbers), the P4 session event, ``VoiceCallEnded``.
* ``recording_ready``: only when the dial-time gate requested recording -> ``recording_fetch_status='pending'`` and the
  fetch task (``recordings.fetch`` re-checks policy and consent). A recording the gate did not request is never fetched:
  it is deleted at the provider.
"""

import hmac
import ipaddress
import uuid
from collections.abc import Mapping
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.events.telephony import TelephonyProviderDeleteQueued, TelephonyRecordingReady, VoiceCallEnded
from app.models.telephony import TERMINAL_CALL_STATUSES, VoiceCall
from app.services.connectors.voice.bridge import OP_CALL_STATUS, CallStatusDTO, billable_minutes
from app.services.telephony import common

_RANK = {"requested": 0, "dialing_recruiter": 1, "dialing_candidate": 2, "in_progress": 3}
_TERMINAL = {"completed": "completed", "no_answer": "no_answer", "busy": "busy", "failed": "failed"}
OUTCOME_LABEL = {"completed": "completed", "no_answer": "no answer", "busy": "busy", "failed": "failed", "cancelled": "cancelled"}


async def find_call(db: AsyncSession, provider_key: str, external_ref: str) -> VoiceCall | None:
    if not external_ref:
        return None
    return (
        await db.execute(
            select(VoiceCall)
            .where(VoiceCall.external_ref == external_ref, VoiceCall.provider_key.in_((provider_key, "fake")))
            .with_for_update()
        )
    ).scalar_one_or_none()


async def intercept(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any], config: Any) -> bool:
    """The ``voice`` inbound handler hook: True when the payload belongs to a telephony call (handled here)."""
    ref = str(payload.get("external_ref") or "")
    provider_key = str(getattr(config, "provider_key", "") or "")
    call = await find_call(db, provider_key, ref)
    if call is None:
        return False
    event = dict(payload)
    if payload.get("verify_by_fetch") or call.provider_key == "exotel":
        fetched = await fetch_status(ctx, db, call)
        if fetched is None:
            return True  # nothing trusted from the callback
        event = fetched
    await apply_event(ctx, db, call, event)
    return True


async def fetch_status(ctx: RequestContext, db: AsyncSession, call: VoiceCall) -> dict[str, Any] | None:
    """Verify-by-fetch: the provider's current status as a normalised event (None when unavailable)."""
    from app.models.connectors import ProviderConnectorConfig
    from app.services.connectors.registry import build_for

    if call.config_id is None or not call.external_ref:
        return None
    cfg = await db.get(ProviderConnectorConfig, call.config_id)
    try:
        adapter = await build_for(ctx, db, common.CATEGORY, cfg)
        dto = CallStatusDTO.from_mapping(await adapter.handle(OP_CALL_STATUS, {"external_ref": call.external_ref}))
    except Exception as exc:
        logger.info("Call status fetch for {} failed: {}", call.id, type(exc).__name__)
        return None
    event: dict[str, Any] = {"external_ref": call.external_ref, "kind": dto.status, "leg": "agent", "duration_s": dto.duration_s, "price_minor": dto.price_minor, "currency": dto.currency}
    if dto.status == "answered" and call.provider_key == "exotel":
        event["kind"] = "candidate_answered"  # Exotel connects both legs before reporting in-progress
    if dto.recording_ref and dto.status == "completed":
        event["recording_ref"] = dto.recording_ref
        event["recording_channels"] = dto.recording_channels
    return event


async def apply_event(ctx: RequestContext, db: AsyncSession, call: VoiceCall, event: Mapping[str, Any]) -> str:
    from app.services import voice_service

    kind = str(event.get("kind") or "")
    if kind == "recording_ready":
        await _recording_ready(db, call, event)
        return call.status
    if call.status in TERMINAL_CALL_STATUSES or not kind:
        return call.status
    now = common.now()
    if kind == "ringing":
        if _RANK.get(call.status, 9) < 1:
            call.status = "dialing_recruiter"
    elif kind == "answered":
        if _RANK.get(call.status, 9) < 2:
            call.status = "dialing_candidate"
    elif kind == "candidate_answered":
        if _RANK.get(call.status, 9) < 3:
            call.status = "in_progress"
            call.answered_at = call.answered_at or now
            await voice_service.on_provider_event(ctx, db, call.voice_session_id, "started")
    elif kind in _TERMINAL:
        await _terminal(ctx, db, call, _TERMINAL[kind], event)
        if event.get("recording_ref"):
            await _recording_ready(db, call, event)
    await db.flush()
    return call.status


async def _terminal(ctx: RequestContext, db: AsyncSession, call: VoiceCall, status: str, event: Mapping[str, Any]) -> None:
    from app.services import metering_service, voice_service
    from app.services.telephony import costs

    call.status = status
    call.ended_at = common.now()
    duration = event.get("duration_s")
    if status == "completed":
        call.duration_s = int(duration) if duration is not None else call.duration_s
        call.answered_at = call.answered_at or call.ended_at
    minutes = billable_minutes(call.duration_s) if status == "completed" else 0
    call.billable_minutes = minutes
    call.cost_estimate_minor = minutes * call.rate_per_minute_minor
    call.reserved_minor = 0
    if event.get("price_minor") is not None:
        call.provider_price_minor = int(event["price_minor"])
        call.price_currency = (str(event.get("currency") or "") or None) and str(event.get("currency"))[:3]
    await db.flush()
    if minutes:
        await metering_service.record(
            RequestContext(tenant_id=call.tenant_id), "telephony_minute", minutes, call.installation_id,
            route_group=f"le:{call.legal_entity_id or 'default'}|user:{call.placed_by}",
        )  # fmt: skip
    if status == "completed":
        await voice_service.on_provider_event(
            ctx, db, call.voice_session_id, "ended", duration_s=call.duration_s, awaiting_recording=call.recording_requested
        )
    else:
        await voice_service.on_provider_event(ctx, db, call.voice_session_id, "not_connected")
    await _activity(ctx, db, call)
    emit(db, VoiceCallEnded(
        tenant_id=call.tenant_id, call_id=call.id, voice_session_id=call.voice_session_id, candidate_id=call.candidate_id,
        application_id=call.application_id, outcome=status, duration_s=call.duration_s, billable_minutes=minutes,
    ))  # fmt: skip
    audit.record("telephony.call_ended", "voice_call", call.id, session=db, outcome=status, duration_s=call.duration_s)
    settings_row = await _settings(db)
    await costs.check_alerts(db, settings_row)


async def _settings(db: AsyncSession) -> Any:
    from app.services.telephony import settings as settings_mod

    return await settings_mod.row(db)


def _mmss(seconds: int | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


async def _activity(ctx: RequestContext, db: AsyncSession, call: VoiceCall) -> None:  # noqa: ARG001
    """The ``call`` entry of the communication history (Candidate and Application); never a phone number."""
    from app.services import activity_service

    outcome = OUTCOME_LABEL.get(call.status, call.status)
    body = f"Outbound screening call — {outcome}, {_mmss(call.duration_s)}"
    data = {
        "kind": "voice_call", "voice_call_id": str(call.id), "voice_session_id": str(call.voice_session_id),
        "human_id": call.human_id, "direction": "outbound", "duration_s": call.duration_s, "outcome": call.status,
        "recorded": call.recording_requested, "recording_available": call.recording_fetch_status == "stored",
        "transcript_id": None,
    }  # fmt: skip
    actx = RequestContext(tenant_id=call.tenant_id, actor_kind="system")
    try:
        async with db.begin_nested():
            if call.candidate_id is not None:
                call.activity_id = await activity_service.add_system_entry(actx, db, "candidate", call.candidate_id, body, data, kind="call")
            if call.application_id is not None:
                aid = await activity_service.add_system_entry(actx, db, "application", call.application_id, body, data, kind="call")
                call.activity_id = call.activity_id or aid
    except Exception:  # pragma: no cover - the call outcome stands without the history entry
        logger.exception("Call activity for {} not written", call.id)


async def _recording_ready(db: AsyncSession, call: VoiceCall, event: Mapping[str, Any]) -> None:
    ref = str(event.get("recording_ref") or "")
    if not ref or call.recording_fetch_status in ("pending", "stored"):
        return
    call.provider_recording_ref = ref[:300]
    if call.recording_requested:
        call.recording_fetch_status = "pending"
        await db.flush()
        emit(db, TelephonyRecordingReady(tenant_id=call.tenant_id, call_id=call.id))
        return
    # never requested by the gate: never fetched, deleted at the provider
    call.recording_fetch_status = "skipped"
    call.provider_delete_status = "pending"
    call.provider_delete_first_at = common.now()
    await db.flush()
    emit(db, TelephonyProviderDeleteQueued(tenant_id=call.tenant_id, call_id=call.id))


# ------------------------------------------------------------------------------------------- Exotel relay
async def relay_exotel(
    config_id: uuid.UUID, path_token: str, headers: Mapping[str, str], body: bytes, client_ip: str | None
) -> Any:
    """Exotel cannot sign callbacks: check the secret path token (constant time) and the configured Exotel CIDRs,
    then hand the body to the connector ingress with the relay HMAC (``X-TalentOS-Relay``). 401 otherwise."""
    from app.core.database import tenant_session
    from app.core.errors import Unauthorized
    from app.models.connectors import ProviderConnectorConfig
    from app.services import connectors_service
    from app.services.connectors.registry import webhook_secret
    from app.services.connectors.service import tenant_for_hook
    from app.services.connectors.voice.exotel import RELAY_HEADER
    from app.services.connectors.voice.live_base import relay_signature

    tenant_id = await tenant_for_hook(config_id)
    if tenant_id is None:
        raise common.not_found("Webhook")
    async with tenant_session(tenant_id) as db:
        row = await db.get(ProviderConnectorConfig, config_id)
        if row is None or row.provider_key != "exotel" or row.category != common.CATEGORY:
            raise common.not_found("Webhook")
        secret = await webhook_secret(RequestContext(tenant_id=tenant_id), db, row)
        cidrs = list((row.settings or {}).get("callback_ip_cidrs") or [])
    if not secret or not hmac.compare_digest(secret.encode(), path_token.encode()):
        raise Unauthorized("Invalid webhook signature", code="webhook_signature_invalid")
    if cidrs and not _ip_in(client_ip, cidrs):
        raise Unauthorized("Invalid webhook signature", code="webhook_signature_invalid")
    relayed = {k: v for k, v in headers.items() if k.lower() != RELAY_HEADER.lower()}
    relayed[RELAY_HEADER] = relay_signature(secret, body)
    return await connectors_service.ingest_webhook(common.CATEGORY, config_id, relayed, body, client_ip=client_ip)


def _ip_in(client_ip: str | None, cidrs: list[str]) -> bool:
    if not client_ip:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
        return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)
    except ValueError:
        return False


# ------------------------------------------------------------------------------------------- answer endpoint
async def answer(
    config_id: uuid.UUID, call_token: str, url: str, headers: Mapping[str, str], body: bytes
) -> tuple[str, str]:
    """The provider fetches the answer document (TwiML / Plivo XML): the provider signature is verified for the
    rebuilt URL, the call is found by the token hash (valid ``telephony_answer_token_ttl_seconds``); 404 otherwise."""
    from app.core.database import tenant_session
    from app.core.errors import NotFound, Unauthorized
    from app.core.integrations import find_manifest
    from app.core.signing import verify_hmac
    from app.models.connectors import ProviderConnectorConfig
    from app.models.telephony import TelephonyNumber
    from app.services.connectors.registry import webhook_secret
    from app.services.connectors.service import tenant_for_hook
    from app.services.telephony import gates, twiml
    from app.services.telephony import settings as settings_mod

    tenant_id = await tenant_for_hook(config_id)
    if tenant_id is None:
        raise NotFound("Not found")
    ctx = RequestContext(tenant_id=tenant_id)
    async with tenant_session(tenant_id) as db:
        cfg = await db.get(ProviderConnectorConfig, config_id)
        if cfg is None or cfg.category != common.CATEGORY:
            raise NotFound("Not found")
        manifest = find_manifest(cfg.provider_key)
        spec = manifest.webhook if manifest is not None else None
        secret = await webhook_secret(ctx, db, cfg)
        if spec is None or spec.scheme not in ("hmac_sha1_url", "plivo_v3") or not secret:
            raise NotFound("Not found")
        if not verify_hmac(spec.scheme, secret.encode(), headers, body, url, signature_header=spec.signature_header, timestamp_header=spec.timestamp_header):
            raise Unauthorized("Invalid webhook signature", code="webhook_signature_invalid")
        call = (
            await db.execute(select(VoiceCall).where(VoiceCall.callback_token_hash == common.token_hash(call_token), VoiceCall.config_id == config_id))
        ).scalar_one_or_none()
        if call is None or call.token_expires_at is None or call.token_expires_at < common.now() or call.candidate_id is None:
            raise NotFound("Not found")
        number = await db.get(TelephonyNumber, call.number_id) if call.number_id else None
        phones = await gates.candidate_phones(db, call.candidate_id)
        if number is None or not phones:
            raise NotFound("Not found")
        s = await settings_mod.row(db)
        from app.services.telephony.calls import _tenant_name

        notice = s.recording_notice_text.replace("{tenant}", await _tenant_name(db)) if call.recording_requested else None
        hook = url.split("/answer/")[0]
        doc = twiml.render(
            cfg.provider_key, candidate_e164=phones[0], caller_id_e164=number.number_e164, notice=notice,
            record=call.recording_requested, callback_url=hook, max_duration_s=s.max_call_minutes * 60,
        )  # fmt: skip
        if doc is None:
            raise NotFound("Not found")
        return doc
