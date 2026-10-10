# ruff: noqa: E501
"""Epic 76 (stories 76.2 / 76.4 / 76.5): the pre-call gate, ★ placement, the Fake bridged lifecycle, recordings (fetch,
provider delete, withdrawal), STT, costs and the recruiter phone. Fakes only."""

import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden, Unauthorized
from app.models.activity import Activity
from app.models.candidates import CandidatePhone
from app.models.files import StoredFile
from app.models.tasks import Task
from app.models.telephony import SttJob, TelephonySettings, UserCallPhone, VoiceCall
from app.models.voice import VoiceSession
from app.schemas.telephony import PlaceCallIn, TelephonySettingsIn
from app.services import telephony_service as svc
from app.services.connectors.voice.fake import FakeBridgedVoiceProvider
from app.services.telephony import common, ingress, recordings, stt_jobs
from tests.telephony.conftest import AGENT_PHONE, CANDIDATE_PHONE, TOrg, human, install


async def _place(t: TOrg, **kw: Any) -> Any:
    async with tenant_session(t.tenant_id) as db:
        return await svc.place_call(human(t.org, **kw), db, PlaceCallIn(application_id=t.org.application_id))


async def _refused(t: TOrg) -> str:
    with pytest.raises(ConflictError) as exc:
        await _place(t)
    return exc.value.code


async def _call(t: TOrg, call_id: uuid.UUID) -> VoiceCall:
    async with tenant_session(t.tenant_id) as db:
        row = await db.get(VoiceCall, call_id)
        assert row is not None
        db.expunge(row)
        return row


async def _session(t: TOrg, sid: uuid.UUID) -> VoiceSession:
    async with tenant_session(t.tenant_id) as db:
        row = await db.get(VoiceSession, sid)
        assert row is not None
        db.expunge(row)
        return row


async def _feed(t: TOrg, call_id: uuid.UUID, events: list[dict[str, Any]] | None = None) -> None:
    """Deliver the Fake's scripted events one transaction each (as the ingress does)."""
    from app.models.connectors import ProviderConnectorConfig
    from app.services.connectors.registry import load_config

    call = await _call(t, call_id)
    evs = events if events is not None else FakeBridgedVoiceProvider().bridged_events(call.external_ref or "")
    for ev in evs:
        async with tenant_session(t.tenant_id) as db:
            cfg = await load_config(db, await db.get(ProviderConnectorConfig, t.config_id))  # type: ignore[arg-type]
            assert await ingress.intercept(human(t.org), db, ev, cfg) is True


async def _settings(t: TOrg, **values: Any) -> None:
    async with tenant_session(t.tenant_id) as db:
        await svc.update_settings(human(t.org), db, TelephonySettingsIn(**values))


# ------------------------------------------------------------------------------------------- happy path
async def test_recorded_call_lifecycle_recording_stt_and_costs(torg: TOrg) -> None:
    t = torg
    await install(t.org, "azure_speech", {"subscription_key": "k"})
    await _settings(t, stt_enabled=True)
    async with tenant_session(t.tenant_id) as db:
        opts = await svc.call_options(human(t.org), db, application_id=t.org.application_id)
    assert opts.can_call and opts.recording_will_be == "recorded" and opts.phone_last4 == "0001"
    assert opts.caller_id_label == "Acme Bengaluru (+918040000000)" and opts.provider_key == "twilio"
    out = await _place(t)
    assert (out.status, out.recorded, out.provider_key) == ("dialing_recruiter", True, "twilio")
    dumped = out.model_dump_json()
    assert AGENT_PHONE not in dumped and CANDIDATE_PHONE not in dumped and "FKC-" not in dumped
    sent = FakeBridgedVoiceProvider.bridged[(await _call(t, out.id)).external_ref or ""]
    assert sent["record"] is True and sent["payload"]["notice_text"].startswith("This call is recorded")
    assert sent["payload"]["status_callback_url"] == f"https://hooks.example.com/api/v1/hooks/voice/{t.config_id}"
    sess = await _session(t, out.voice_session_id)
    assert (sess.status, sess.mode, sess.provider_key) == ("scheduled", "call", "twilio")
    # concurrency: a second call while this one is open
    assert await _refused(t) in ("call_in_progress", "voice_session_open")
    await _feed(t, out.id)
    call = await _call(t, out.id)
    assert (call.status, call.duration_s, call.billable_minutes, call.cost_estimate_minor, call.reserved_minor) == ("completed", 75, 2, 200, 0)
    # recording fetched (eager tasks), stored in tenant storage, provider copy deleted, STT ran
    assert call.recording_fetch_status == "stored" and call.provider_delete_status == "deleted"
    assert FakeBridgedVoiceProvider.deleted == [f"FKREC-{call.external_ref}"]
    sess = await _session(t, out.voice_session_id)
    assert sess.recording_file_id is not None and sess.recording_allowed
    assert sess.transcript_id is not None and sess.transcript_source == "provider"
    async with tenant_session(t.tenant_id) as db:
        job = (await db.execute(select(SttJob))).scalar_one()
        assert (job.source_kind, job.status, job.channels) == ("voice_call", "completed", 2)
        acts = list((await db.execute(select(Activity).where(Activity.kind == "call"))).scalars())
        assert {a.entity_type for a in acts} == {"candidate", "application"}
        for a in acts:
            assert a.body.startswith("Outbound screening call — completed, 01:15")
            blob = f"{a.body}{a.data}"
            assert AGENT_PHONE not in blob and CANDIDATE_PHONE not in blob and "8040000000" not in blob
        file = await db.get(StoredFile, sess.recording_file_id)
        assert file is not None and file.owner_entity_type == "voice_session"
    async with tenant_session(t.tenant_id) as db:
        usage = await svc.usage(human(t.org), db, group_by="user")
    assert usage.rows[0].minutes == 2 and usage.rows[0].cost_estimate_minor == 200 and usage.today_cost_minor >= 200
    got = await _call(t, out.id)
    async with tenant_session(t.tenant_id) as db:
        listed = await svc.list_calls(human(t.org), db, voice_session_id=out.voice_session_id)
    assert listed.total == 1 and listed.items[0].transcript_status == "completed" and got.activity_id is not None


async def test_out_of_order_and_duplicate_events_never_regress(torg: TOrg) -> None:
    out = await _place(torg)
    ref = (await _call(torg, out.id)).external_ref
    await _feed(torg, out.id, [{"external_ref": ref, "kind": "completed", "duration_s": 30}])
    await _feed(torg, out.id, [{"external_ref": ref, "kind": "ringing"}, {"external_ref": ref, "kind": "candidate_answered"}, {"external_ref": ref, "kind": "no_answer"}])
    call = await _call(torg, out.id)
    assert (call.status, call.billable_minutes) == ("completed", 1)
    async with tenant_session(torg.tenant_id) as db:
        assert len(list((await db.execute(select(Activity).where(Activity.kind == "call", Activity.entity_type == "candidate"))).scalars())) == 1


@pytest.mark.parametrize(("suffix", "status"), [("0002", "no_answer"), ("0003", "busy"), ("0004", "failed"), ("0005", "no_answer")])
async def test_fake_scripts_not_connected(torg: TOrg, suffix: str, status: str) -> None:
    async with tenant_session(torg.tenant_id) as db:
        await db.execute(update(CandidatePhone).values(phone_e164=f"+91981111{suffix}"))
    out = await _place(torg)
    await _feed(torg, out.id)
    call = await _call(torg, out.id)
    assert call.status == status and call.cost_estimate_minor == 0
    assert (await _session(torg, out.voice_session_id)).status == "planned"  # the recruiter may retry


# ------------------------------------------------------------------------------------------- gates
async def test_gate_refusals_in_order(torg: TOrg) -> None:
    t = torg
    from app.services import ecosystem_service

    # 2 kill switch
    async with tenant_session(t.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human(t.org), db, on=True, reason="incident")
    assert await _refused(t) == "ecosystem_suspended"
    async with tenant_session(t.tenant_id) as db:
        await ecosystem_service.set_kill_switch(human(t.org), db, on=False, reason="resolved")
    # 8 calling hours (21:30 IST) with the next window
    common.set_clock(lambda: datetime(2026, 10, 12, 16, 0, tzinfo=UTC))
    with pytest.raises(ConflictError) as exc:
        await _place(t)
    assert exc.value.code == "outside_calling_hours" and exc.value.details["next_window_at"].startswith("2026-10-13T09:00")
    common.set_clock(lambda: datetime(2026, 10, 12, 6, 30, tzinfo=UTC))
    # 10 cost cap (reservation of one max-length call)
    await _settings(t, daily_cost_cap_minor=1000)
    assert await _refused(t) == "cost_cap_reached"
    await _settings(t, daily_cost_cap_minor=200000)
    # 7 do-not-call
    from app.services.messaging.opt_outs import record

    async with tenant_session(t.tenant_id) as db:
        await record(db, "voice", CANDIDATE_PHONE, "user")
    assert await _refused(t) == "do_not_call"
    async with tenant_session(t.tenant_id) as db:
        from app.models.comms import ChannelOptOut

        await db.execute(update(ChannelOptOut).values(revoked_at=datetime.now(UTC)))
    # 11 no phone
    async with tenant_session(t.tenant_id) as db:
        await db.execute(update(CandidatePhone).values(phone_e164="12"))
    assert await _refused(t) == "no_phone"
    async with tenant_session(t.tenant_id) as db:
        await db.execute(update(CandidatePhone).values(phone_e164=CANDIDATE_PHONE))
    # 5 phone not verified
    async with tenant_session(t.tenant_id) as db:
        await svc.remove_phone(human(t.org), db)
    assert await _refused(t) == "call_phone_not_verified"


async def test_no_provider_and_consent_required(torg: TOrg) -> None:
    from app.models.telephony import TelephonyNumber

    async with tenant_session(torg.tenant_id) as db:
        await db.execute(update(TelephonyNumber).values(status="inactive"))
    assert await _refused(torg) == "no_voice_provider"
    async with tenant_session(torg.tenant_id) as db:
        await db.execute(update(TelephonyNumber).values(status="active"))
        from app.services.privacy import consent as pc

        await pc.withdraw_consent(human(torg.org), db, torg.org.candidate_id, "voice_screening")
    assert await _refused(torg) == "voice_consent_required"


@pytest.mark.parametrize("kind", ["app", "api_key", "agent", "system"])
async def test_only_a_person_places_a_call(torg: TOrg, kind: str) -> None:
    import dataclasses

    ctx = human(torg.org, principal_type="app" if kind in ("app", "api_key") else "user")
    if kind == "api_key":
        ctx = dataclasses.replace(ctx, principal=dataclasses.replace(ctx.principal, type="api_key"))  # type: ignore[arg-type]
    if kind in ("agent", "system"):
        ctx = dataclasses.replace(ctx, actor_kind=kind)
    async with tenant_session(torg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await svc.place_call(ctx, db, PlaceCallIn(application_id=torg.org.application_id))
    assert exc.value.code == "human_only_action"
    assert FakeBridgedVoiceProvider.bridged == {}


def test_agent_tool_providers_stay_fake_and_manual() -> None:
    from app.services.agent_tools.voice import tools_e58

    assert tools_e58.ALLOWED_PROVIDERS == ("fake", "manual")


# ------------------------------------------------------------------------------------------- recording decision
async def test_consent_to_call_but_not_record(torg: TOrg) -> None:
    async with tenant_session(torg.tenant_id) as db:
        from app.services.privacy import consent as pc

        await pc.withdraw_consent(human(torg.org), db, torg.org.candidate_id, "call_recording")
    out = await _place(torg)
    assert (out.recorded, out.recording_unavailable_reason) == (False, "consent_missing")
    call = await _call(torg, out.id)
    assert FakeBridgedVoiceProvider.bridged[call.external_ref or ""]["record"] is False
    # a recording the gate never requested is never fetched; the provider copy is deleted
    await _feed(torg, out.id, [{"external_ref": call.external_ref, "kind": "recording_ready", "recording_ref": "FKREC-x"}])
    call = await _call(torg, out.id)
    assert (call.recording_fetch_status, call.provider_delete_status) == ("skipped", "deleted")
    assert "fetch_recording" not in [op for op, _ in FakeBridgedVoiceProvider.calls]


async def test_policy_not_permitted_is_unrecorded(torg: TOrg) -> None:
    from app.models.candidates import Candidate

    async with tenant_session(torg.tenant_id) as db:
        await db.execute(update(Candidate).where(Candidate.id == torg.org.candidate_id).values(location={"country": "ZZ"}))
    out = await _place(torg)
    assert (out.recorded, out.recording_unavailable_reason) == (False, "policy_not_permitted")


async def test_withdrawal_stops_fetch_and_deletes_provider_copy(torg: TOrg) -> None:
    out = await _place(torg)
    call = await _call(torg, out.id)
    async with tenant_session(torg.tenant_id) as db:
        row = await db.get(VoiceCall, out.id)
        assert row is not None
        row.provider_recording_ref = "FKREC-held"
        row.recording_fetch_status = "pending"
    async with tenant_session(torg.tenant_id) as db:
        from app.services.privacy import consent as pc

        await pc.withdraw_consent(human(torg.org), db, torg.org.candidate_id, "call_recording")
    call = await _call(torg, out.id)
    assert (call.recording_fetch_status, call.provider_delete_status) == ("skipped", "deleted")
    assert "FKREC-held" in FakeBridgedVoiceProvider.deleted


async def test_provider_delete_failure_retries_then_notifies(torg: TOrg) -> None:
    from datetime import timedelta

    out = await _place(torg)
    async with tenant_session(torg.tenant_id) as db:
        row = await db.get(VoiceCall, out.id)
        assert row is not None
        row.provider_recording_ref, row.provider_delete_status = "FKREC-z", "pending"
    FakeBridgedVoiceProvider.fail_ops.add("voice.delete_recording")
    async with tenant_session(torg.tenant_id) as db:
        assert await recordings.delete_provider(human(torg.org), db, out.id) == "pending"
    common.set_clock(lambda: datetime(2026, 10, 12, 6, 30, tzinfo=UTC) + timedelta(hours=73))
    async with tenant_session(torg.tenant_id) as db:
        assert await recordings.delete_provider(human(torg.org), db, out.id) == "failed"
    async with tenant_session(torg.tenant_id) as db:
        titles = [t.title for t in (await db.execute(select(Task))).scalars()]
    assert any("Delete the recording" in x for x in titles)


# ------------------------------------------------------------------------------------------- STT
async def test_stt_failure_falls_back_to_manual(torg: TOrg) -> None:
    from app.services.connectors.stt.fake import FakeSpeechToText

    await install(torg.org, "azure_speech", {"subscription_key": "k"})
    await _settings(torg, stt_enabled=True)
    FakeSpeechToText.fail_ops.add("transcribe")
    out = await _place(torg)
    await _feed(torg, out.id)  # the eager fetch queues the job; attempt 1 fails
    async with tenant_session(torg.tenant_id) as db:
        job = (await db.execute(select(SttJob))).scalar_one()
    assert (job.status, job.attempts) == ("queued", 1)
    for _ in range(4):
        async with tenant_session(torg.tenant_id) as db:
            await stt_jobs.run(human(torg.org), db, job.id)
    async with tenant_session(torg.tenant_id) as db:
        job = (await db.execute(select(SttJob))).scalar_one()
        titles = [t.title for t in (await db.execute(select(Task))).scalars()]
    assert job.status == "failed"
    assert (await _session(torg, out.voice_session_id)).status == "uploaded"
    assert any("Upload a transcript or enter answers" in x for x in titles)


async def test_upload_resolver_queues_stt(torg: TOrg) -> None:
    from tests.agents.voice.helpers import request, upload_recording

    await install(torg.org, "azure_speech", {"subscription_key": "k"})
    await _settings(torg, stt_enabled=True)
    sid = await request(torg.org, mode="upload")
    resp = await upload_recording(torg.org, sid)
    assert resp.status_code == 200, resp.text
    async with tenant_session(torg.tenant_id) as db:
        job = (await db.execute(select(SttJob))).scalar_one()
    assert (job.source_kind, job.status) == ("voice_upload", "completed")
    assert (await _session(torg, sid)).transcript_source == "provider"


# ------------------------------------------------------------------------------------------- costs
async def test_cost_alert_and_cap(torg: TOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(common, "fan_out", lambda tid, key, payload: sent.append(key))
    from app.services.telephony import costs as costs_mod

    monkeypatch.setattr(costs_mod.common, "fan_out", lambda tid, key, payload: sent.append(key))
    await _settings(torg, daily_cost_cap_minor=3100, cost_alert_pct=80)  # one 30-min call reserves 3000
    out = await _place(torg)
    assert "telephony_cost_threshold" in sent and "telephony_cost_cap_reached" not in sent
    await _feed(torg, out.id)
    assert await _refused(torg) == "cost_cap_reached"  # 200 spent + 3000 reservation > 3100
    async with tenant_session(torg.tenant_id) as db:
        opts = await svc.call_options(human(torg.org), db, application_id=torg.org.application_id)
    assert opts.today_cost_pct >= 6


# ------------------------------------------------------------------------------------------- phones
async def test_phone_verification_flow(torg: TOrg) -> None:
    from app.services.telephony import user_phones

    async with tenant_session(torg.tenant_id) as db:
        out = await svc.start_phone(human(torg.org), db, "98765 43210")
    assert (out.status, out.last4) == ("pending", "3210")
    for _ in range(5):
        async with tenant_session(torg.tenant_id) as db:
            with pytest.raises(Exception) as exc:
                await svc.verify_phone(human(torg.org), db, "000000" if torg.otp[-1] != "000000" else "111111")
        assert getattr(exc.value, "code", "") == "wrong_code"
    async with tenant_session(torg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc2:
            await svc.verify_phone(human(torg.org), db, torg.otp[-1])
    assert exc2.value.code == "too_many_attempts"
    async with tenant_session(torg.tenant_id) as db:
        row = (await db.execute(select(UserCallPhone).where(UserCallPhone.status == "pending"))).scalar_one()
        assert b"9876543210" not in bytes(row.phone_cipher) and row.phone_last4 == "3210"
        opened = await user_phones.verified_phone(db, torg.org.admin.user_id)
    assert opened is None
    async with tenant_session(torg.tenant_id) as db:
        att = await svc.attest_phone(human(torg.org), db, torg.org.admin.user_id, "+919876543210", "No SMS provider yet")
    assert (att.status, att.verification_method, att.last4) == ("verified", "admin_attested", "3210")


# ------------------------------------------------------------------------------------------- signatures / answer
def _twilio_sig(token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


async def test_twilio_callback_signature_and_answer_document(torg: TOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import urlencode

    from app.services.connectors.service import ingest_webhook

    monkeypatch.setattr("app.services.telephony.calls.secrets.token_urlsafe", lambda n: "fixed-answer-token")
    out = await _place(torg)
    # answer document over HTTP (provider-signed, no session)
    url = f"https://hooks.example.com/api/v1/hooks/voice/{torg.config_id}/answer/fixed-answer-token"
    params = {"CallSid": "CA1", "CallStatus": "in-progress"}
    client = torg.org.admin.client
    resp = await client.post(f"/api/v1/hooks/voice/{torg.config_id}/answer/fixed-answer-token", content=urlencode(params),
                             headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": _twilio_sig("twilio-auth-token", url, params)})  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert "<Say" in resp.text and 'record="record-from-answer-dual"' in resp.text and CANDIDATE_PHONE in resp.text and AGENT_PHONE not in resp.text
    bad = await client.post(f"/api/v1/hooks/voice/{torg.config_id}/answer/fixed-answer-token", content=urlencode(params),
                            headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "nope"})  # fmt: skip
    assert bad.status_code == 401
    unknown = await client.post(f"/api/v1/hooks/voice/{torg.config_id}/answer/other", content=urlencode(params),
                                headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": _twilio_sig("twilio-auth-token", url.replace("fixed-answer-token", "other"), params)})  # fmt: skip
    assert unknown.status_code == 404
    # status callback through the foundation ingress: Twilio HMAC over the rebuilt URL; tampering -> 401
    async with tenant_session(torg.tenant_id) as db:
        from app.models.connectors import ProviderConnectorConfig

        await db.execute(update(ProviderConnectorConfig).where(ProviderConnectorConfig.id == torg.config_id).values(settings={}))
    hook = f"https://hooks.example.com/api/v1/hooks/voice/{torg.config_id}"
    form = {"CallSid": "CA-unknown", "CallStatus": "ringing", "SequenceNumber": "0"}
    body = urlencode(form).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": _twilio_sig("twilio-auth-token", hook, form)}
    ack = await ingest_webhook("voice", torg.config_id, headers, body)
    assert ack.status == "accepted"
    dup = await ingest_webhook("voice", torg.config_id, headers, body)
    assert dup.status == "duplicate"
    with pytest.raises(Unauthorized):
        await ingest_webhook("voice", torg.config_id, headers, body.replace(b"ringing", b"completed"))
    assert out.status == "dialing_recruiter"


async def test_cross_tenant_reads_are_not_found(torg: TOrg, app: Any) -> None:
    from app.core.errors import NotFound
    from tests.agents.voice.helpers import make_vorg

    out = await _place(torg)
    other = await make_vorg(app)
    async with tenant_session(other.tenant_id) as db:
        with pytest.raises(NotFound):
            await svc.get_call(human(other), db, out.id)
    await other.admin.client.aclose()


async def test_settings_validation(torg: TOrg) -> None:
    from datetime import time

    from app.core.errors import ValidationFailed

    async with tenant_session(torg.tenant_id) as db:
        with pytest.raises(ValidationFailed):
            await svc.update_settings(human(torg.org), db, TelephonySettingsIn(call_window_start=time(21, 0), call_window_end=time(9, 0)))
    with pytest.raises(ValueError):
        TelephonySettingsIn(cost_alert_pct=40)
    async with tenant_session(torg.tenant_id) as db:
        assert (await db.execute(select(TelephonySettings))).scalar_one().call_window_start == time(9, 0)
