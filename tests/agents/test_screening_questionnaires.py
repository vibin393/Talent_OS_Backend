# ruff: noqa: E501
"""Epic 57: questionnaire versions / bindings (57.1), ``extract_answers`` (A4, used by Epic 58), the erasure handler."""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.core.settings import get_settings
from app.models.pipeline import ScreeningQuestionnaire
from app.models.screening_agent import ScreeningAnswer, ScreeningConversation, ScreeningTurn
from app.schemas.screening_agent import BindingIn, ExtractAnswersIn, QuestionDraft, VersionCreate
from app.schemas.transcripts import TranscriptSegmentIn
from app.services import screening_agent_service as svc
from app.services import transcripts_service
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.screening_agent import turns
from tests.agents.screening_helpers import API, SOrg, make_sorg
from tests.modules.identity.conftest import csrf


@pytest.fixture(autouse=True)
def _no_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    # single gateway calls (pre-screen, extraction) need a configured key; the client is always the Fake LLM here
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "fake-test-key")


@pytest.fixture
async def sorg(app: FastAPI) -> SOrg:
    return await make_sorg(app)


async def _questionnaire(org: SOrg) -> uuid.UUID:
    from app.services.pipeline import screening as p1

    async with tenant_session(org.tenant_id) as db:
        return (await p1.ensure_default_questionnaire(db)).id


async def test_versions_approval_and_binding(sorg: SOrg) -> None:
    org = sorg
    qid = await _questionnaire(org)
    c = org.admin.client
    v1 = await c.post(f"{API}/screening-questionnaires/{qid}/versions", json={}, headers=csrf(c))
    assert v1.status_code == 201, v1.text
    body = v1.json()
    assert body["version"] == 1 and body["status"] == "draft" and len(body["questions"]) >= 5
    ids = {q["key"]: q["id"] for q in body["questions"]}
    # knockout criteria are validated per type
    bad = [*body["questions"], {"key": "notes", "label": "Anything else?", "type": "text", "knockout": {"op": "eq", "value": "x"}}]
    resp = await c.patch(f"{API}/screening-questionnaire-versions/{body['id']}", json={"questions": bad}, headers=csrf(c))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "knockout_invalid", resp.text
    good = [
        *body["questions"],
        {"key": "years_python", "label": "Years of Python?", "type": "number", "unit": "years", "required": True, "knockout": {"op": "lt", "value": 3}, "origin": "ai_generated"},
    ]
    resp = await c.patch(f"{API}/screening-questionnaire-versions/{body['id']}", json={"questions": good}, headers=csrf(c))
    assert resp.status_code == 200, resp.text
    # AI-suggested questions must be accepted before approval
    resp = await c.post(f"{API}/screening-questionnaire-versions/{body['id']}/approve", headers=csrf(c))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "question_not_accepted", resp.text
    py_id = next(q["id"] for q in (await c.get(f"{API}/screening-questionnaires/{qid}/versions")).json()[0]["questions"] if q["key"] == "years_python")
    resp = await c.post(f"{API}/screening-questionnaire-versions/{body['id']}/questions/{py_id}/accept", headers=csrf(c))
    assert resp.status_code == 200, resp.text
    resp = await c.post(f"{API}/screening-questionnaire-versions/{body['id']}/approve", headers=csrf(c))
    assert resp.status_code == 200 and resp.json()["status"] == "approved", resp.text
    # a second version keeps question ids stable and retires v1 on approval
    v2 = (await c.post(f"{API}/screening-questionnaires/{qid}/versions", json={"change_note": "v2"}, headers=csrf(c))).json()
    assert {q["key"]: q["id"] for q in v2["questions"]}["interest"] == ids["interest"]
    resp = await c.post(f"{API}/screening-questionnaire-versions/{v2['id']}/approve", headers=csrf(c))
    assert resp.status_code == 200
    versions = (await c.get(f"{API}/screening-questionnaires/{qid}/versions")).json()
    assert [v["status"] for v in versions] == ["approved", "retired"]
    # approved versions cannot be edited
    resp = await c.patch(f"{API}/screening-questionnaire-versions/{v2['id']}", json={"change_note": "x"}, headers=csrf(c))
    assert resp.status_code == 409
    # binding: tenant-wide -> used for the application
    resp = await c.put(f"{API}/screening-questionnaires/{qid}/bindings", json={"scope_kind": "tenant"}, headers=csrf(c))
    assert resp.status_code == 200, resp.text
    async with tenant_session(org.tenant_id) as db:
        ver = await svc.resolve_for_application(org.ctx(), db, org.application_id)
        assert ver is not None and ver.id == uuid.UUID(v2["id"])
    resp = await c.delete(f"{API}/screening-questionnaires/{qid}/bindings", params={"scope_kind": "tenant"}, headers=csrf(c))
    assert resp.status_code == 204


async def test_default_questionnaire_is_auto_approved_per_demand(sorg: SOrg) -> None:
    org = sorg
    async with tenant_session(org.tenant_id) as db:
        ver = await svc.resolve_for_application(org.ctx(), db, org.application_id)
        assert ver is not None and ver.status == "approved" and ver.approved_by == org.admin.user_id
        keys = {q["key"] for q in ver.questions}
        assert {"interest", "notice_period", "expected_compensation"} <= keys
        qn = await db.get(ScreeningQuestionnaire, ver.questionnaire_id)
        assert qn is not None and not qn.is_default
        again = await svc.resolve_for_application(org.ctx(), db, org.application_id)
        assert again is not None and again.id == ver.id


def test_question_validation() -> None:
    from app.services.screening_agent.questionnaires import knockout_hit, validate_questions

    qid = uuid.uuid4()
    out = validate_questions(qid, [QuestionDraft(key="notice", label="Notice", type="number", unit="days", knockout={"op": "gt", "value": 90})])
    assert knockout_hit(out[0], 120) and not knockout_hit(out[0], 30)
    with pytest.raises(ValidationFailed):
        validate_questions(qid, [QuestionDraft(key="c", label="Pick", type="choice")])
    with pytest.raises(ValidationFailed):
        validate_questions(qid, [QuestionDraft(key="b", label="Yes?", type="boolean", knockout={"op": "lt", "value": True})])
    assert BindingIn(scope_kind="tenant").scope_id is None
    with pytest.raises(ValueError):
        BindingIn(scope_kind="demand")
    assert VersionCreate().questions is None


# ================================================================ extract_answers
async def _conversation_with_transcript(org: SOrg) -> tuple[ScreeningConversation, list[dict]]:
    async with tenant_session(org.tenant_id) as db:
        conv = await svc.conversations.start(org.ctx(), db, org.application_id, channel="link_chat", trigger_kind="workflow", run_id=uuid.uuid4())
        conv.status = "in_progress"
        await turns.add_turn(db, conv, direction="outbound", author="agent", kind="question", body="What is your notice period?")
        await turns.add_turn(db, conv, direction="inbound", author="candidate", kind="answer", body="around 2 months, and yes I am interested")
        from app.models.screening_agent import ScreeningQuestionnaireVersion

        ver = await db.get(ScreeningQuestionnaireVersion, conv.questionnaire_version_id)
        db.expunge(conv)
        return conv, list(ver.questions)  # type: ignore[union-attr]


async def test_extract_answers_normalises_flags_and_supersedes(sorg: SOrg) -> None:
    org = sorg
    conv, qs = await _conversation_with_transcript(org)
    by = {q["key"]: q for q in qs}
    async with tenant_session(org.tenant_id) as db:
        from app.models.screening_agent import ScreeningQuestionnaireVersion

        ver = await db.get(ScreeningQuestionnaireVersion, conv.questionnaire_version_id)
        questions = [dict(q) for q in ver.questions]  # type: ignore[union-attr]
        for q in questions:
            if q["key"] == "notice_period":
                q["knockout"] = {"op": "gt", "value": 30}
        ver.questions = questions  # type: ignore[union-attr]
    req = ExtractAnswersIn(application_id=org.application_id, questionnaire_id=ver.questionnaire_id, questionnaire_version=ver.version, transcript_id=conv.transcript_id, source="text")  # type: ignore[union-attr,arg-type]
    first = {
        "answers": [
            {"question_id": by["notice_period"]["id"], "quote": "around 2 months", "value": 60, "confidence": 0.5, "segment_refs": [2]},
            {"question_id": by["availability"]["id"], "quote": "next month", "value": "next month", "confidence": 0.9, "segment_refs": [2]},
            {"question_id": str(uuid.uuid4()), "quote": "x", "value": 1, "confidence": 0.9},
        ],
        "unanswered": [],
    }
    with fake_llm({"screening_extract": [FakeTurn("emit_result", result=first)]}):
        async with tenant_session(org.tenant_id) as db:
            out = await svc.extract_answers(org.ctx(), db, req)
    found = {str(a.question_id): a for a in out.answers}
    notice = found[by["notice_period"]["id"]]
    assert notice.normalized == {"value": 60, "unit": "days"} and set(notice.flags) == {"knockout", "low_confidence"}
    assert notice.segment_refs == (2,)
    avail = found[by["availability"]["id"]]
    assert avail.normalized is None and "invalid_value" in avail.flags and float(avail.confidence) <= 0.3
    assert len(out.answers) == 2  # an unknown question id is ignored
    second = {"answers": [{"question_id": by["notice_period"]["id"], "quote": "60", "value": 60, "confidence": 0.95}], "unanswered": []}
    with fake_llm({"screening_extract": [FakeTurn("emit_result", result=second)]}):
        async with tenant_session(org.tenant_id) as db:
            out = await svc.extract_answers(org.ctx(), db, req)
    notice = next(a for a in out.answers if str(a.question_id) == by["notice_period"]["id"])
    assert float(notice.confidence) == 0.95 and "low_confidence" not in notice.flags
    async with tenant_session(org.tenant_id) as db:
        rows = (await db.execute(select(ScreeningAnswer).where(ScreeningAnswer.question_id == uuid.UUID(by["notice_period"]["id"])))).scalars().all()
        assert len(rows) == 2 and sum(1 for r in rows if r.superseded_at is None) == 1
    # gateway failure -> empty result, every question unanswered
    with fake_llm({"screening_extract": [FakeTurn("error", error="provider_error")]}):
        async with tenant_session(org.tenant_id) as db:
            out = await svc.extract_answers(org.ctx(), db, req)
    assert out.answers == () and len(out.unanswered) == len(qs)


async def test_extract_answers_voice_uses_candidate_segments_only(sorg: SOrg) -> None:
    org = sorg
    async with tenant_session(org.tenant_id) as db:
        ver = await svc.resolve_for_application(org.ctx(), db, org.application_id)
        t = await transcripts_service.create(org.ctx(), db, source_kind="voice_session", candidate_id=org.candidate_id, application_id=org.application_id)
        await transcripts_service.append_segments(org.ctx(), db, t.id, [
            TranscriptSegmentIn(speaker_role="agent", start_ms=0, end_ms=1000, text="SECRET-AGENT-LINE what is your notice?"),
            TranscriptSegmentIn(speaker_role="candidate", start_ms=1500, end_ms=4000, text="Thirty days."),
        ], final=True)  # fmt: skip
        notice = next(q for q in ver.questions if q["key"] == "notice_period")  # type: ignore[union-attr]
    script = {"screening_extract": [FakeTurn("emit_result", result={"answers": [{"question_id": notice["id"], "quote": "Thirty days.", "value": 30, "confidence": 0.9, "segment_refs": [2]}], "unanswered": []})]}
    with fake_llm(script) as fake:
        async with tenant_session(org.tenant_id) as db:
            out = await svc.extract_answers(org.ctx(), db, ExtractAnswersIn(application_id=org.application_id, questionnaire_id=ver.questionnaire_id, questionnaire_version=ver.version, transcript_id=t.id, source="voice"))  # type: ignore[union-attr]
    sent = str(fake.calls[0]["messages"])
    assert "SECRET-AGENT-LINE" not in sent and "Thirty days." in sent
    assert out.answers[0].start_ms == 1500 and out.answers[0].normalized == {"value": 30, "unit": "days"}
    async with tenant_session(org.tenant_id) as db:
        row = (await db.execute(select(ScreeningAnswer).where(ScreeningAnswer.transcript_id == t.id))).scalar_one()
        assert row.source == "voice" and row.conversation_id is None


# ================================================================ erasure
async def test_erasure_deletes_turns_and_answers(sorg: SOrg) -> None:
    from app.services.privacy.registry import ErasureContext
    from app.services.screening_agent_erasure import ScreeningConversationsHandler

    org = sorg
    conv, _ = await _conversation_with_transcript(org)
    async with tenant_session(org.tenant_id) as db:
        row = await db.get(ScreeningConversation, conv.id)
        row.summary = "Interested."  # type: ignore[union-attr]
    handler = ScreeningConversationsHandler()
    ctx = ErasureContext(tenant_id=org.tenant_id, candidate_id=org.candidate_id, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(org.tenant_id) as db:
        exported = await handler.export(db, ctx)
        assert exported and exported["screening_conversations"][0]["messages"]
        counts = await handler.erase(db, ctx)
        assert counts["screening_turns"] == 2
        assert await handler.verify(db, ctx) == []
        assert (await db.execute(select(ScreeningTurn).where(ScreeningTurn.conversation_id == conv.id))).first() is None
