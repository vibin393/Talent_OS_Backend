# ruff: noqa: E501
"""Epic 58 Voice Screening: consent page + Recording Policy gate, Fake / Manual call paths, uploads, extraction from
candidate lines, review with playback, withdrawal, erasure, Recording Policy admin, permissions and isolation."""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, DomainError
from app.core.settings import get_settings
from app.models.files import StoredFile
from app.models.screening_agent import ScreeningAnswer
from app.models.tasks import Task
from app.models.transcripts import Transcript
from app.models.voice import VoiceSession, VoiceUploadAttestation
from app.schemas.screening_agent import AnswerDecisionIn
from app.services import screening_agent_service
from app.services import voice_service as svc
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.voice import uploads as uploads_mod
from tests.agents.screening_helpers import SOrg
from tests.agents.voice.helpers import (
    API,
    BENIGN,
    UNLABELLED,
    agent_off,
    calls,
    consent,
    done,
    grant,
    h,
    last_link,
    make_vorg,
    q,
    questions,
    request,
    session,
    upload_recording,
    upload_transcript,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    # single gateway calls (extraction) need a configured key; the client is always the Fake LLM here
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "fake-test-key")
    uploads_mod.set_transcription_provider(None)


@pytest.fixture
async def vorg(app: FastAPI) -> SOrg:
    return await make_vorg(app)


def _script() -> dict:
    return {"agent_voice_screening": [], "screening_extract": [], None: [BENIGN] * 20}


async def _tasks(org: SOrg) -> list[str]:
    async with tenant_session(org.tenant_id) as db:
        return list((await db.execute(select(Task.title))).scalars())


def _ready_turns(sid: uuid.UUID) -> list[FakeTurn]:
    s = str(sid)
    return [
        calls(("voice.session_get", {"voice_session_id": s})),
        calls(("voice.extract_answers", {"voice_session_id": s})),
        calls(
            (
                "voice.save_summary",
                {
                    "voice_session_id": s,
                    "summary": "Interested; notice period 60 days.",
                    "gaps": ["Expected compensation not asked"],
                },
            )
        ),
        done("ready_for_review", answered=2),
    ]


def _extraction(qs: list[dict], refs: tuple[int, int] = (2, 4)) -> FakeTurn:
    return FakeTurn("emit_result", result={
        "answers": [
            {"question_id": q(qs, "interest"), "quote": "Yes, I am very interested.", "value": True, "confidence": 0.95, "segment_refs": [refs[0]]},
            {"question_id": q(qs, "notice_period"), "quote": "My notice period is 60 days.", "value": 60, "confidence": 0.9, "segment_refs": [refs[1]]},
        ],
        "unanswered": [],
    })  # fmt: skip


# ================================================================ consent + gate (58.2 / 58.3)
async def test_agent_sends_consent_link_and_page_snapshots_gate(vorg: SOrg) -> None:
    with fake_llm(_script()) as fake:
        sid = await request(
            vorg,
            fake=fake,
            turns=lambda s: [
                calls(("voice.request_consent", {"voice_session_id": str(s), "channel": "email"})),
                done("consent_requested"),
            ],
        )
    row = await session(vorg, sid)
    assert (
        row.status == "consent_pending" and row.human_id.startswith("VS-") and row.consent_link_id is not None
    )
    assert row.questionnaire_id is not None and row.agent_run_id is not None
    token = await last_link(vorg)
    view = await svc.consent_view(token)
    assert {n["purpose"] for n in view["notices"]} == {"voice_screening", "call_recording"}
    assert view["voice_screening"] == "pending" and view["call_recording"] == "pending"
    out = await svc.consent_decide(token, "India", "KA", True, True)
    assert out["voice_screening"] == "granted" and out["call_recording"] == "granted"
    row = await session(vorg, sid)
    assert row.recording_allowed and row.recording_rule == "candidate_consent" and row.jurisdiction == "IN-KA"
    assert row.status == "planned" and row.voice_consent_id and row.recording_consent_id


async def test_recording_refused_and_unlisted_country(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg, voice=True, recording=False)
    row = await session(vorg, sid)
    assert not row.recording_allowed and row.status == "planned"
    resp = await upload_recording(vorg, sid)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "recording_not_permitted"
    assert "missing_consent:call_recording" in resp.json()["error"]["details"]["reasons"]
    # the candidate agrees later but is in a country not listed in the policy -> not permitted
    await grant(vorg, "call_recording")
    async with tenant_session(vorg.tenant_id) as db:
        await db.execute(update(VoiceSession).where(VoiceSession.id == sid).values(confirmed_country="US"))
    resp = await upload_recording(vorg, sid)
    assert resp.status_code == 409 and resp.json()["error"]["details"]["rule"] == "not_permitted"
    # a transcript-only upload is still possible with voice_screening consent
    resp = await upload_transcript(vorg, sid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready_for_review" and not resp.json()["has_recording"]


async def test_call_refused_cancels_and_unknown_country_rejected(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)  # agent off: the domain sends the consent email itself
    with pytest.raises(DomainError):
        await consent(vorg, country="Atlantis")
    await consent(vorg, voice=False, recording=True)
    row = await session(vorg, sid)
    assert row.status == "cancelled" and not row.recording_allowed and row.recording_consent_id is None


async def test_unknown_location_gate_not_permitted(vorg: SOrg) -> None:
    from app.models.candidates import Candidate
    from app.services.recording_policy import gate

    async with tenant_session(vorg.tenant_id) as db:
        await db.execute(update(Candidate).where(Candidate.id == vorg.candidate_id).values(location=None))
        d = await gate.check(vorg.ctx(), db, purpose="call_recording", candidate_id=vorg.candidate_id)
    assert not d.recording_allowed and d.rule in ("unknown", "not_permitted")


# ================================================================ Manual uploads (58.1 / 58.3)
async def test_transcript_upload_needs_consent_and_attestation(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)
    resp = await upload_transcript(vorg, sid)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "voice_consent_missing"
    await consent(vorg, recording=False)
    resp = await upload_transcript(vorg, sid, attest=False)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "attestation_required"
    resp = await upload_transcript(vorg, sid)
    assert resp.status_code == 200, resp.text
    async with tenant_session(vorg.tenant_id) as db:
        att = (
            await db.execute(
                select(VoiceUploadAttestation).where(VoiceUploadAttestation.voice_session_id == sid)
            )
        ).scalar_one()
        assert (
            att.kind == "transcript"
            and att.consent_obtained
            and att.policy_followed
            and att.gate_snapshot["rule"]
        )
        t = await db.get(Transcript, resp.json()["transcript_id"])
        assert t is not None and t.source_kind == "voice_session" and t.attested_by == vorg.admin.user_id


async def test_recording_formats_size_and_recording_only_task(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    resp = await upload_recording(
        vorg, sid, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "video/mp4", "call.mp4"
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unsupported_media"
    disguised = b"\x00\x00\x00\x18ftypmp42" + b"moovtrakhdlr\x00\x00\x00\x00vide" + b"\x00" * 64
    resp = await upload_recording(vorg, sid, disguised, "audio/mp4", "call.m4a")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unsupported_media"
    resp = await vorg.admin.client.put(
        f"{API}/settings/agents/voice-screening", json={"max_upload_mb": 1}, headers=h(vorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["max_upload_mb"] == 1
    big = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * (2 * 1024 * 1024)
    resp = await upload_recording(vorg, sid, big)
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "file_too_large"
    resp = await upload_recording(vorg, sid, b"ID3\x03\x00\x00\x00" + b"\x00" * 256, "audio/mpeg", "call.mp3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["status"] == "uploaded"
        and body["has_recording"]
        and body["recording_media_type"] == "audio/mpeg"
    )
    assert any(t.startswith("Upload a transcript for") for t in await _tasks(vorg))


async def test_unlabelled_transcript_needs_speaker_marking(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    resp = await upload_transcript(vorg, sid, UNLABELLED)
    assert (
        resp.status_code == 200 and resp.json()["status"] == "uploaded" and resp.json()["needs_speaker_label"]
    )
    resp = await vorg.admin.client.get(f"{API}/voice-sessions/{sid}/transcript")
    segs = resp.json()["segments"]
    assert [s["speaker_role"] for s in segs] == ["unknown", "unknown"]
    resp = await vorg.admin.client.post(
        f"{API}/voice-sessions/{sid}/speakers", json={"segment_seqs_as_candidate": [2]}, headers=h(vorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready_for_review" and not resp.json()["needs_speaker_label"]
    roles = [
        s["speaker_role"]
        for s in (await vorg.admin.client.get(f"{API}/voice-sessions/{sid}/transcript")).json()["segments"]
    ]
    assert roles == ["recruiter", "candidate"]


async def test_fake_transcription_provider_only_when_registered(vorg: SOrg) -> None:
    from app.services.connectors.voice.fake import FakeTranscriptionProvider

    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    uploads_mod.set_transcription_provider(FakeTranscriptionProvider())
    try:
        resp = await upload_recording(vorg, sid)
        assert (
            resp.status_code == 200
            and resp.json()["has_transcript"]
            and resp.json()["transcript_source"] == "provider"
        )
    finally:
        uploads_mod.set_transcription_provider(None)


# ================================================================ agent processing + review (58.4 / 58.5)
async def test_ready_run_extracts_from_candidate_lines_and_review(vorg: SOrg) -> None:
    await grant(vorg, "voice_screening", "call_recording")
    async with tenant_session(vorg.tenant_id) as db:
        out = await svc.request(vorg.ctx(), db, vorg.application_id, mode="upload", provider_key="manual")
        sid = out.id
        await db.execute(update(VoiceSession).where(VoiceSession.id == sid).values(confirmed_country="IN"))
    assert out.status == "planned"
    qs = await questions(vorg, sid)
    with fake_llm(_script()) as fake:
        fake._by_prompt["agent_voice_screening"].extend(_ready_turns(sid))
        fake._by_prompt["screening_extract"].append(_extraction(qs))
        assert (await upload_recording(vorg, sid)).status_code == 200
        resp = await upload_transcript(vorg, sid)
        assert resp.status_code == 200, resp.text
        # only candidate lines reach the extraction model
        sent = [c for c in fake.calls if "notice period is 60" in str(c)]
        assert sent and all("Are you interested in this role" not in str(c) for c in sent)
    row = await session(vorg, sid)
    assert (
        row.status == "ready_for_review" and row.summary and row.gaps == ["Expected compensation not asked"]
    )
    resp = await vorg.admin.client.get(f"{API}/screening-reviews", params={"voice_session_id": str(sid)})
    assert resp.status_code == 200, resp.text
    answers = resp.json()["answers"]
    assert len(answers) == 2 and {a["start_ms"] for a in answers} == {5000, 14000}
    # playback of the recording
    resp = await vorg.admin.client.get(f"{API}/voice-sessions/{sid}/playback")
    assert resp.status_code == 200 and resp.json()["url"].endswith("/download")
    assert (await vorg.admin.client.get(resp.json()["url"])).status_code == 200
    # an agent can never complete the review (★)
    agent_ctx = RequestContext(
        tenant_id=vorg.tenant_id, principal=vorg.ctx().principal, actor_kind="agent", actor_id=uuid.uuid4()
    )
    async with tenant_session(vorg.tenant_id) as db:
        with pytest.raises(DomainError):
            await svc.complete_review(agent_ctx, db, sid)
        for a in answers:
            await screening_agent_service.decide_answer(
                vorg.ctx(), db, uuid.UUID(a["id"]), AnswerDecisionIn(decision="accept")
            )
    resp = await vorg.admin.client.post(f"{API}/voice-sessions/{sid}/complete-review", headers=h(vorg.admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reviewed" and resp.json()["screening_id"]


async def test_summary_language_refused() -> None:
    from app.services.voice.agent_ops import check_summary

    for text in ("She sounded nervous on the call", "Candidate seemed honest", "Long pauses before answers"):
        with pytest.raises(DomainError) as exc:
            check_summary([text])
        assert exc.value.code == "summary_language"
    check_summary(["Interested; notice period 60 days."])


# ================================================================ Fake and Manual call paths (58.1)
async def test_fake_call_end_to_end(vorg: SOrg) -> None:
    async with tenant_session(vorg.tenant_id) as db:
        out = await svc.request(vorg.ctx(), db, vorg.application_id, mode="call", provider_key="fake")
        sid = out.id
    qs = await questions(vorg, sid)
    s = str(sid)
    with fake_llm(_script()) as fake:
        async with tenant_session(vorg.tenant_id) as db:
            row = await db.get(VoiceSession, sid)
            from app.services.voice import consent as consent_mod

            await consent_mod.send_request(vorg.ctx(), db, row, origin="user")
        token = await last_link(vorg)
        fake._by_prompt["agent_voice_screening"].extend([
            calls(("voice.schedule_call", {"voice_session_id": s})), done("scheduled"), *_ready_turns(sid),
        ])  # fmt: skip
        fake._by_prompt["screening_extract"].append(_extraction(qs))
        await svc.consent_decide(token, "IN", None, True, True)
    row = await session(vorg, sid)
    assert row.external_ref == f"fake-call:{sid}" and row.transcript_source == "provider"
    assert row.recording_file_id is not None and row.recording_media_type == "audio/wav"
    assert row.status == "ready_for_review", (row.status, row.failure_code)


async def test_manual_call_creates_task(vorg: SOrg) -> None:
    async with tenant_session(vorg.tenant_id) as db:
        out = await svc.request(vorg.ctx(), db, vorg.application_id, mode="call", provider_key="manual")
        sid = out.id
    async with tenant_session(vorg.tenant_id) as db:
        row = await db.get(VoiceSession, sid)
        from app.services.voice import consent as consent_mod

        await consent_mod.send_request(vorg.ctx(), db, row, origin="user")
    token = await last_link(vorg)
    with fake_llm(_script()) as fake:
        fake._by_prompt["agent_voice_screening"].extend(
            [calls(("voice.schedule_call", {"voice_session_id": str(sid)})), done("manual_required")]
        )
        await svc.consent_decide(token, "IN", None, True, False)
    row = await session(vorg, sid)
    assert row.status == "planned" and row.external_ref is None
    assert any(t.startswith("Call ") and t.endswith("upload the transcript") for t in await _tasks(vorg))


async def test_port_contract_both_adapters() -> None:
    from app.services.connectors.base import ManualActionRequired
    from app.services.connectors.voice.port import CallSessionRequestDTO, VoiceProvider
    from app.services.voice.provider_jobs import adapter

    req = CallSessionRequestDTO(
        uuid.uuid4(), uuid.uuid4(), "+919800000000", None, True, None, 600, None, None
    )
    fake, manual = adapter("fake"), adapter("manual")
    assert isinstance(fake, VoiceProvider) and isinstance(manual, VoiceProvider)
    call = await fake.schedule_call(req)
    ended = await fake.end_call(call.external_ref)
    assert ended.recording_ref and (await fake.fetch_recording(ended.recording_ref)).data.startswith(b"RIFF")
    dto = await fake.fetch_transcript(ended.transcript_ref)
    assert dto is not None and {s.speaker for s in dto.segments} >= {"candidate"}
    assert [e.kind for e in fake.lifecycle(call.external_ref)] == [
        "scheduled",
        "started",
        "ended",
        "recording_ready",
        "transcript_ready",
    ]
    with pytest.raises(ManualActionRequired):
        await manual.schedule_call(req)
    assert await manual.fetch_recording("x") is None and await manual.fetch_transcript("x") is None


# ================================================================ withdrawal, erasure, constraints
async def test_withdrawals_purge_recording_then_transcript(vorg: SOrg) -> None:
    from app.services.privacy import consent as pc

    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    assert (await upload_recording(vorg, sid)).status_code == 200
    assert (await upload_transcript(vorg, sid)).status_code == 200
    file_id = (await session(vorg, sid)).recording_file_id
    sys_ctx = RequestContext(tenant_id=vorg.tenant_id, actor_kind="system")
    async with tenant_session(vorg.tenant_id) as db:
        await pc.withdraw_consent(sys_ctx, db, vorg.candidate_id, "call_recording", by_kind="candidate")
    row = await session(vorg, sid)
    assert (
        row.recording_file_id is None and row.recording_deleted_at is not None and not row.recording_allowed
    )
    assert row.transcript_id is not None and row.status == "ready_for_review"
    async with tenant_session(vorg.tenant_id) as db:
        assert (await db.get(StoredFile, file_id)).deleted_at is not None  # type: ignore[union-attr]
    assert (await vorg.admin.client.get(f"{API}/voice-sessions/{sid}/playback")).status_code == 404
    async with tenant_session(vorg.tenant_id) as db:
        await pc.withdraw_consent(sys_ctx, db, vorg.candidate_id, "voice_screening", by_kind="candidate")
    row = await session(vorg, sid)
    assert row.status == "cancelled" and row.withdrawn_at is not None
    async with tenant_session(vorg.tenant_id) as db:
        t = await db.get(Transcript, row.transcript_id)
        assert t is not None and t.status == "erased"
        assert (
            await db.execute(select(ScreeningAnswer).where(ScreeningAnswer.voice_session_id == sid))
        ).first() is None


async def test_sweep_purges_recording_without_consent(vorg: SOrg) -> None:
    from app.models.privacy import Consent
    from app.services.voice import withdrawal

    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    assert (await upload_recording(vorg, sid)).status_code == 200
    async with tenant_session(vorg.tenant_id) as db:  # a withdrawal whose task never ran
        await db.execute(
            update(Consent)
            .where(Consent.candidate_id == vorg.candidate_id, Consent.purpose == "call_recording")
            .values(status="withdrawn")
        )
    async with tenant_session(vorg.tenant_id) as db:
        assert await withdrawal.sweep(db) >= 1
    assert (await session(vorg, sid)).recording_file_id is None


async def test_check_backs_recording_allowed(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    assert (await upload_recording(vorg, sid)).status_code == 200
    with pytest.raises(IntegrityError):
        async with tenant_session(vorg.tenant_id) as db:
            await db.execute(
                update(VoiceSession).where(VoiceSession.id == sid).values(recording_allowed=False)
            )


async def test_erasure_handler_scrubs_sessions(vorg: SOrg) -> None:
    from app.services.privacy.registry import handlers
    from app.services.voice_erasure import VoiceSessionsHandler

    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    await upload_transcript(vorg, sid)
    async with tenant_session(vorg.tenant_id) as db:
        await db.execute(update(VoiceSession).where(VoiceSession.id == sid).values(summary="x"))
    assert any(x.name == "voice.sessions" and x.order == 67 for x in handlers())

    class Ctx:
        subject_kind = "candidate"
        candidate_id = vorg.candidate_id
        retained: dict = {}  # noqa: RUF012

    async with tenant_session(vorg.tenant_id) as db:
        await VoiceSessionsHandler().erase(db, Ctx())  # type: ignore[arg-type]
        assert await VoiceSessionsHandler().verify(db, Ctx()) == []  # type: ignore[arg-type]
    row = await session(vorg, sid)
    assert row.summary is None and row.confirmed_country is None


# ================================================================ API, policy admin, isolation
async def test_api_request_rules_and_cross_tenant(vorg: SOrg, app: FastAPI) -> None:
    await agent_off(vorg)
    resp = await vorg.admin.client.post(
        f"{API}/applications/{vorg.application_id}/voice-sessions", json={"mode": "upload", "provider_key": "manual"},
        headers={**h(vorg.admin), "Idempotency-Key": str(uuid.uuid4())},
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    assert resp.json()["status"] == "consent_pending" and resp.json()["voice_consent"] == "pending"
    resp = await vorg.admin.client.post(
        f"{API}/applications/{vorg.application_id}/voice-sessions", json={"mode": "upload"},
        headers={**h(vorg.admin), "Idempotency-Key": str(uuid.uuid4())},
    )  # fmt: skip
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "voice_session_open"
    listed = (await vorg.admin.client.get(f"{API}/applications/{vorg.application_id}/voice-sessions")).json()
    assert [x["id"] for x in listed] == [sid]
    other = await make_vorg(app)
    assert (await other.admin.client.get(f"{API}/voice-sessions/{sid}")).status_code == 404
    assert (
        await other.admin.client.post(f"{API}/voice-sessions/{sid}/cancel", headers=h(other.admin))
    ).status_code == 404
    resp = await vorg.admin.client.post(f"{API}/voice-sessions/{sid}/cancel", headers=h(vorg.admin))
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"


async def test_recording_policy_admin(vorg: SOrg) -> None:
    c = vorg.admin.client
    rules = (await c.get(f"{API}/recording-policies")).json()
    assert any(
        r["country"] == "IN" and r["region"] is None and r["rule"] == "candidate_consent" for r in rules
    )
    resp = await c.put(
        f"{API}/recording-policies",
        json={"country": "US", "region": "CA", "rule": "all_party_consent", "note": "Counsel memo"},
        headers=h(vorg.admin),
    )
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]
    ev = (await c.get(f"{API}/recording-policies/evaluate", params={"country": "US", "region": "ca"})).json()
    assert ev["rule"] == "all_party_consent" and ev["matched"] == "region"
    ev = (await c.get(f"{API}/recording-policies/evaluate", params={"country": "US", "region": "NY"})).json()
    assert ev["rule"] == "not_permitted" and ev["matched"] == "none"
    ev = (await c.get(f"{API}/recording-policies/evaluate", params={"country": "India"})).json()
    assert ev["rule"] == "candidate_consent"
    assert (await c.delete(f"{API}/recording-policies/{rid}", headers=h(vorg.admin))).status_code == 204
    from app.core.audit import AuditLog

    async with tenant_session(vorg.tenant_id) as db:
        actions = set(
            (
                await db.execute(select(AuditLog.action).where(AuditLog.entity_type == "recording_policy"))
            ).scalars()
        )
    assert {"recording_policy.changed", "recording_policy.deleted"} <= actions


async def test_settings_and_permission_catalogue(vorg: SOrg) -> None:
    from app.core.permissions import CATALOG, DEFAULT_ROLES

    assert CATALOG["voice:upload"].sensitive
    roles = {r.key: r for r in DEFAULT_ROLES}
    assert "voice:upload" in roles["recruiter"].permissions
    resp = await vorg.admin.client.get(f"{API}/settings/agents/voice-screening")
    assert resp.status_code == 200 and resp.json()["default_provider"] == "manual"


async def test_no_tool_accepts_audio() -> None:
    from app.core.agentic import tools

    voice_tools = {
        k: t for k, t in tools().items() if k.startswith(("voice.", "transcription.", "recording_policy."))
    }
    assert len(voice_tools) == 9
    for key, tool in voice_tools.items():
        for name, field in tool.input_model.model_fields.items():
            assert field.annotation is not bytes, key
            assert not any(w in name for w in ("audio", "file", "recording", "pitch", "tone")), (key, name)
    from app.core.agentic import agents

    spec = agents()["voice_screening"]
    assert not spec.placeholder and spec.owner_epic == "58" and set(spec.tools) >= set(voice_tools)


async def test_cancel_conflicts_after_review_state(vorg: SOrg) -> None:
    await agent_off(vorg)
    sid = await request(vorg)
    await consent(vorg)
    await upload_transcript(vorg, sid)
    async with tenant_session(vorg.tenant_id) as db:
        with pytest.raises(ConflictError):
            await svc.cancel(vorg.ctx(), db, sid)
