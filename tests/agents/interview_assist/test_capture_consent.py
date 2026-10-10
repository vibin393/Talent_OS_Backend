# ruff: noqa: E501
"""Epic 60 stories 60.1 / 60.2: consent and Recording Policy gating, acknowledgements, uploads and speakers."""

import uuid

import httpx
from sqlalchemy import update

from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.models.candidates import Candidate
from app.models.interview_captures import InterviewCapture
from app.models.transcripts import TranscriptSegment
from app.services import interview_captures_service as svc
from tests.agents.interview_assist.helpers import (
    API,
    TRANSCRIPT,
    UNKNOWN_SPEAKERS,
    Seed,
    ack,
    consented,
    ctx_for,
    grant_recording,
    h,
    rows,
    upload,
)


async def _status(seed: Seed, user: uuid.UUID | None = None) -> object:
    ctx = await ctx_for(seed.tenant_id, user or seed.ann.user_id)
    async with tenant_session(seed.tenant_id) as db:
        return await svc.capture_status(ctx, db, seed.interview_id)


async def _request(seed: Seed) -> object:
    ctx = await ctx_for(seed.tenant_id, seed.ann.user_id)
    async with tenant_session(seed.tenant_id) as db:
        return await svc.request_capture(ctx, db, seed.interview_id)


# ================================================================ 60.1 gating
async def test_capture_waits_for_every_participant(seed: Seed, outbox: OutboxMailer) -> None:
    out = await _request(seed)
    st = out.status  # type: ignore[attr-defined]
    assert st.status == "consent_pending" and not st.offered
    assert "missing_consent:candidate" in st.reasons
    assert f"missing_ack:{seed.p_ann}" in st.reasons and f"missing_ack:{seed.p_bob}" in st.reasons
    assert "Waiting for the candidate to agree" in st.reason_texts
    assert "candidate" in out.sent  # type: ignore[attr-defined]
    assert any(m.kind == "interview_recording_consent" for m in outbox.sent)
    # India + candidate_consent: the candidate's consent alone is not enough -- every interviewer must agree too.
    await grant_recording(seed.tenant_id, seed.candidate_id)
    await ack(seed.tenant_id, seed.ann.user_id)
    st = await _status(seed)
    assert st.status == "consent_pending" and st.reasons == [f"missing_ack:{seed.p_bob}"]  # type: ignore[attr-defined]
    assert st.reason_texts == ["Waiting for Bob Iyer to agree"]  # type: ignore[attr-defined]
    await ack(seed.tenant_id, seed.bob.user_id)
    st = await _status(seed)
    assert st.status == "planned" and st.offered and st.reasons == []  # type: ignore[attr-defined]


async def test_jurisdiction_not_permitted_and_unknown(seed: Seed) -> None:
    await consented(seed)
    async with tenant_session(seed.tenant_id) as db:
        await db.execute(
            update(Candidate)
            .where(Candidate.id == seed.candidate_id)
            .values(location={"country": "United States"})
        )
    st = await _status(seed)
    assert st.status == "blocked" and "not_permitted:US" in st.reasons and not st.offered  # type: ignore[attr-defined]
    async with tenant_session(seed.tenant_id) as db:
        await db.execute(update(Candidate).where(Candidate.id == seed.candidate_id).values(location=None))
    st = await _status(seed)
    assert st.status == "blocked" and "unknown_jurisdiction" in st.reasons  # type: ignore[attr-defined]


async def test_standing_ack_api_and_revoke(seed: Seed) -> None:
    resp = await seed.ann.client.get(f"{API}/me/interview-recording-ack")
    assert (
        resp.status_code == 200
        and resp.json()["acked"] is False
        and "everyone agrees" in resp.json()["statement"]
    )
    resp = await seed.ann.client.post(f"{API}/me/interview-recording-ack", headers=h(seed.ann))
    assert resp.status_code == 200 and resp.json()["acked"] is True
    await grant_recording(seed.tenant_id, seed.candidate_id)
    await ack(seed.tenant_id, seed.bob.user_id)
    await _request(seed)
    assert (await _status(seed)).status == "planned"  # type: ignore[attr-defined]
    resp = await seed.ann.client.delete(f"{API}/me/interview-recording-ack", headers=h(seed.ann))
    assert resp.status_code == 200 and resp.json()["acked"] is False
    st = await _status(seed)
    assert st.status == "consent_pending" and f"missing_ack:{seed.p_ann}" in st.reasons  # type: ignore[attr-defined]


async def test_external_interviewer_acks_through_link(
    app: object, agent_org: object, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    from tests.agents.interview_assist.helpers import seed_interview

    seed = await seed_interview(app, agent_org.admin, agent_org.tenant, agent_org.candidate_id, external=True)  # type: ignore[attr-defined]
    try:
        await consented(seed)
        st = await _status(seed)
        assert st.status == "consent_pending" and st.reasons == [f"missing_ack:{seed.p_ext}"]  # type: ignore[attr-defined]
        assert "Waiting for Eli External to agree" in st.reason_texts  # type: ignore[attr-defined]
        mail = next(m for m in outbox.sent if m.kind == "interview_capture_ack_requested")
        token = mail.text.split("/l/")[1].split()[0]
        view = await public.get(f"{API}/public/interview-capture-ack/{token}")
        assert view.status_code == 200 and view.json()["status"] == "waiting"
        frame = await public.get(f"{API}/public/links/{token}")
        assert frame.status_code == 200 and frame.json()["purpose"] == "interview_capture_ack"
        agreed = await public.post(f"{API}/public/interview-capture-ack/{token}", json={"agree": True})
        assert agreed.status_code == 200 and agreed.json()["status"] == "agreed"
        assert (await _status(seed)).status == "planned"  # type: ignore[attr-defined]
    finally:
        await seed.ann.client.aclose()
        await seed.bob.client.aclose()


async def test_candidate_consent_link_confirms_location(
    seed: Seed, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    async with tenant_session(seed.tenant_id) as db:
        await db.execute(update(Candidate).where(Candidate.id == seed.candidate_id).values(location=None))
    await ack(seed.tenant_id, seed.ann.user_id)
    await ack(seed.tenant_id, seed.bob.user_id)
    await _request(seed)
    mail = next(m for m in outbox.sent if m.kind == "interview_recording_consent")
    token = mail.text.split("/l/")[1].split()[0]
    view = await public.get(f"{API}/public/interview-recording-consent/{token}")
    assert view.status_code == 200 and view.json()["status"] == "waiting"
    assert view.json()["notice_title"] == "Recording or transcribing your interview"
    no_country = await public.post(
        f"{API}/public/interview-recording-consent/{token}", json={"decision": "agree"}
    )
    assert no_country.status_code == 422
    agreed = await public.post(
        f"{API}/public/interview-recording-consent/{token}",
        json={"decision": "agree", "country": "India", "region": "TN"},
    )
    assert agreed.status_code == 200 and agreed.json()["status"] == "agreed"
    st = await _status(seed)
    assert st.status == "planned" and st.jurisdiction == "IN-TN"  # type: ignore[attr-defined]
    refused = await public.post(
        f"{API}/public/interview-recording-consent/{token}", json={"decision": "refuse"}
    )
    assert refused.status_code == 200 and refused.json()["status"] == "refused"


async def test_capture_api_permissions_and_cross_tenant(seed: Seed, other_org: object) -> None:
    resp = await seed.ann.client.get(f"{API}/interviews/{seed.interview_id}/capture")
    assert (
        resp.status_code == 200
        and resp.json()["status"] == "not_requested"
        and resp.json()["can_manage"] is True
    )
    resp = await seed.ann.client.post(f"{API}/interviews/{seed.interview_id}/capture", headers=h(seed.ann))
    assert resp.status_code == 200 and resp.json()["status"]["status"] == "consent_pending"
    other = other_org.admin  # type: ignore[attr-defined]
    resp = await other.client.get(f"{API}/interviews/{seed.interview_id}/capture")
    assert resp.status_code == 404


# ================================================================ 60.2 uploads
async def test_transcript_upload_maps_speakers_and_emits_ready(seed: Seed) -> None:
    await consented(seed)
    result = await upload(seed)
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.status == "processing" and capture.capture_kind == "transcript" and capture.speakers == []
    assert capture.transcript_id == result["transcript_id"] and capture.attested_at is not None
    segs = await rows(
        seed.tenant_id,
        TranscriptSegment,
        TranscriptSegment.transcript_id == capture.transcript_id,
        order_by=TranscriptSegment.seq,
    )
    assert [s.speaker_role for s in segs] == ["interviewer", "candidate", "interviewer", "candidate"]
    assert segs[0].speaker_ref == seed.ann.user_id and segs[1].text.startswith("I built a data pipeline")


async def test_upload_needs_attestation_and_consent(seed: Seed) -> None:
    files = {"file": ("teams.vtt", TRANSCRIPT.encode(), "text/vtt")}
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/transcript",
        files=files,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "capture_not_requested"
    await _request(seed)
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/transcript",
        files=files,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "capture_not_permitted"
    await consented(seed)
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/transcript",
        files=files,
        data={"attest": "false"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "attestation_required"
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/transcript",
        files=files,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"]["status"] == "processing"


async def test_video_refused_and_recording_only_asks_for_transcript(seed: Seed) -> None:
    await consented(seed)
    video = {"file": ("call.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "video/mp4")}
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/recording",
        files=video,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unsupported_media"
    disguised = {
        "file": (
            "call.m4a",
            b"\x00\x00\x00\x18ftypmp42" + b"moovtrakhdlr\x00\x00\x00\x00vide" + b"\x00" * 64,
            "audio/mp4",
        )
    }
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/recording",
        files=disguised,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unsupported_media"
    from app.services.connectors.voice.fake import silence_wav

    wav = {"file": ("call.wav", silence_wav(), "audio/wav")}
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/recording",
        files=wav,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["needs_transcript"] is True and resp.json()["status"]["status"] == "uploaded"
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert (
        capture.file_id is not None and capture.recording_allowed and capture.file_media_type == "audio/wav"
    )
    await upload(seed)
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.capture_kind == "both" and capture.status == "processing"


async def test_fake_transcription_provider_path(seed: Seed) -> None:
    from app.services.connectors.voice.fake import FakeTranscriptionProvider, silence_wav

    svc.set_transcription_provider(FakeTranscriptionProvider())
    await consented(seed)
    ctx = await ctx_for(seed.tenant_id, seed.ann.user_id)
    async with tenant_session(seed.tenant_id) as db:
        out = await svc.upload_recording(
            ctx,
            db,
            seed.interview_id,
            filename="a.wav",
            data=silence_wav(),
            content_type="audio/wav",
            attest=True,
        )
    assert out["transcript_id"] is not None and out["needs_transcript"] is False
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.status == "processing" and capture.capture_kind == "both"


async def test_unknown_speaker_needs_mapping(seed: Seed) -> None:
    await consented(seed)
    await upload(seed, UNKNOWN_SPEAKERS, "notes.txt")
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.status == "uploaded" and capture.speakers == ["Speaker 2"]
    resp = await seed.ann.client.get(f"{API}/interviews/{seed.interview_id}/capture")
    assert resp.json()["unknown_speakers"] == ["Speaker 2"]
    bad = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/speakers",
        json={"candidate_speaker": "Nobody"},
        headers=h(seed.ann),
    )
    assert bad.status_code == 422
    ok = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/speakers",
        json={"candidate_speaker": "Speaker 2"},
        headers=h(seed.ann),
    )
    assert (
        ok.status_code == 200 and ok.json()["status"] == "processing" and ok.json()["unknown_speakers"] == []
    )
    segs = await rows(
        seed.tenant_id,
        TranscriptSegment,
        TranscriptSegment.transcript_id == capture.transcript_id,
        order_by=TranscriptSegment.seq,
    )
    assert [s.speaker_role for s in segs] == ["interviewer", "candidate"]


async def test_settings_api(seed: Seed) -> None:
    admin = seed.admin
    resp = await admin.client.get(f"{API}/settings/agents/interview-assistant")
    assert resp.status_code == 200 and resp.json()["show_suggestions_by_default"] is False
    resp = await admin.client.put(
        f"{API}/settings/agents/interview-assistant",
        json={"allow_transcript_upload": False, "consent_link_days": 3},
        headers=h(admin),
    )
    assert resp.status_code == 200 and resp.json()["consent_link_days"] == 3
    assert (await seed.ann.client.get(f"{API}/settings/agents/interview-assistant")).status_code == 403
    await consented(seed)
    files = {"file": ("teams.vtt", TRANSCRIPT.encode(), "text/vtt")}
    resp = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/capture/transcript",
        files=files,
        data={"attest": "true"},
        headers=h(seed.ann),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "transcript_upload_disabled"
