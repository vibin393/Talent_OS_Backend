# ruff: noqa: E501
"""Epic 60 stories 60.3 - 60.5: the assistant run, outputs, visibility, drafts and decisions, skill suggestions,
withdrawal and erasure."""

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import DomainError, Forbidden
from app.core.permissions import CATALOG, Principal, Scope
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.models.interview_captures import (
    InterviewCapture,
    InterviewCaptureOutput,
    InterviewScorecardDraft,
    ScorecardSuggestionDecision,
)
from app.models.skills_graph import CandidateSkillEvidence, SkillSuggestion
from app.models.transcripts import Transcript
from app.services import interview_captures_service as svc
from tests.agents.interview_assist.helpers import (
    API,
    DONE,
    Seed,
    consented,
    draft_call,
    h,
    happy_script,
    rows,
    run_agent,
    summary_call,
    upload,
)


async def _ready(seed: Seed) -> None:
    await consented(seed)
    await upload(seed)


async def _happy(seed: Seed) -> uuid.UUID:
    await _ready(seed)
    ref = await run_agent(seed, happy_script(seed))
    assert ref.run_id is not None, ref
    run = (await rows(seed.tenant_id, AgentRun, AgentRun.id == ref.run_id))[0]
    assert run.status == "completed", (run.status, run.reason_code)
    return ref.run_id


def _full_ctx(seed: Seed, user_id: uuid.UUID) -> RequestContext:
    principal = Principal(
        type="user", id=user_id, tenant_id=seed.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)
    )
    return RequestContext(tenant_id=seed.tenant_id, principal=principal)


# ================================================================ 60.3 / 60.5 run
async def test_happy_path_outputs_drafts_and_suggestions(seed: Seed) -> None:
    run_id = await _happy(seed)
    calls = {c.tool_key: c for c in await rows(seed.tenant_id, AgentToolCall, AgentToolCall.run_id == run_id)}
    assert (
        calls["interview_capture.save_summary"].outcome == "executed"
        and calls["interview_capture.save_summary"].mode == "auto"
    )
    assert calls["scorecards.create_draft"].outcome == "executed"
    assert calls["interview_capture.transcript"].outcome == "executed"
    run = (await rows(seed.tenant_id, AgentRun, AgentRun.id == run_id))[0]
    assert run.tainted  # the transcript is untrusted
    out = (
        await rows(
            seed.tenant_id, InterviewCaptureOutput, InterviewCaptureOutput.capture_id == seed.capture_id
        )
    )[0]
    assert out.status == "ready" and [s["competency_key"] for s in out.summary_by_competency] == [
        "python",
        "communication",
    ]
    assert (
        out.summary_by_competency[0]["label"] == "Python"
        and out.summary_by_competency[0]["evidence"][0]["start_ms"] == 6000
    )
    assert out.qa_items[0]["kit_question_text"] == "Tell me about a Python project you built."
    drafts = await rows(
        seed.tenant_id, InterviewScorecardDraft, InterviewScorecardDraft.capture_id == seed.capture_id
    )
    assert {d.user_id for d in drafts} == {seed.ann.user_id, seed.bob.user_id}
    assert all(d.recommendation is None for d in drafts)
    sugg = await rows(
        seed.tenant_id, SkillSuggestion, SkillSuggestion.source_ref == f"interview_capture:{seed.capture_id}"
    )
    assert sugg and all(s.source == "interview" and s.status == "pending" for s in sugg)
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.status == "ready" and capture.agent_run_id == run_id


async def test_visibility_drafts_private_outputs_blind_rule(seed: Seed) -> None:
    await _happy(seed)
    mine = await seed.ann.client.get(f"{API}/interviews/{seed.interview_id}/scorecard-draft")
    assert mine.status_code == 200 and mine.json()["recommendation"] is None
    assert (
        mine.json()["show_suggestions_by_default"] is False
        and mine.json()["ratings"][0]["evidence"][0]["quote"]
    )
    bob = await seed.bob.client.get(f"{API}/interviews/{seed.interview_id}/scorecard-draft")
    assert bob.status_code == 200 and bob.json()["id"] != mine.json()["id"]
    assert (
        await seed.admin.client.get(f"{API}/interviews/{seed.interview_id}/scorecard-draft")
    ).status_code == 404
    out = await seed.ann.client.get(f"{API}/interviews/{seed.interview_id}/capture/outputs")
    assert (
        out.status_code == 200
        and out.json()["ai_generated"] is True
        and out.json()["skill_suggestion_count"] >= 1
    )
    # The recruiter (outcome permission, not a required participant) follows the P1 blind rule: may see.
    assert (
        await seed.admin.client.get(f"{API}/interviews/{seed.interview_id}/capture/outputs")
    ).status_code == 200
    opened = await seed.ann.client.post(
        f"{API}/interviews/{seed.interview_id}/scorecard-draft/opened", headers=h(seed.ann)
    )
    assert opened.status_code == 200 and opened.json()["status"] == "opened"


async def test_recommendation_is_always_empty_db_check(seed: Seed) -> None:
    await _happy(seed)
    with pytest.raises(IntegrityError):
        async with tenant_session(seed.tenant_id) as db:
            await db.execute(
                update(InterviewScorecardDraft)
                .where(InterviewScorecardDraft.capture_id == seed.capture_id)
                .values(recommendation="yes")
            )


async def test_validation_kit_keys_scale_quotes_and_language(seed: Seed) -> None:
    await _ready(seed)
    subject = ("interview", seed.interview_id)

    async def save(data: dict[str, object]) -> None:
        async with tenant_session(seed.tenant_id) as db:
            await svc.agent_save_summary(db, subject, None, seed.capture_id, data)  # type: ignore[arg-type]

    good = summary_call(seed)[1]
    with pytest.raises(DomainError) as exc:
        await save(
            {**good, "summary_by_competency": [{"competency_key": "java", "summary": "x", "evidence": []}]}
        )
    assert exc.value.code == "unknown_competency"
    with pytest.raises(DomainError) as exc:
        await save(summary_call(seed, quote="I invented Python")[1])
    assert exc.value.code == "invalid_evidence"
    with pytest.raises(DomainError) as exc:
        await save({**good, "overall_summary": "She sounded confident and honest throughout."})
    assert exc.value.code == "summary_language"
    with pytest.raises(DomainError) as exc:
        await save({**good, "qa_items": [{**good["qa_items"][0], "kit_question_key": "q_unknown"}]})  # type: ignore[index]
    assert exc.value.code == "unknown_question"
    await save(good)
    args = draft_call(seed, seed.p_ann, python=9)[1]
    with pytest.raises(DomainError) as exc:
        async with tenant_session(seed.tenant_id) as db:
            await svc.agent_create_draft(db, subject, None, seed.capture_id, seed.p_ann, args["ratings"], [])  # type: ignore[arg-type]
    assert exc.value.code == "rating_out_of_scale"
    other = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    with pytest.raises(DomainError):
        async with tenant_session(seed.tenant_id) as db:
            await svc.agent_save_summary(db, ("interview", uuid.uuid4()), None, other.id, good)


async def test_rerun_supersedes_outputs(seed: Seed) -> None:
    await _ready(seed)
    script = [
        {"kind": "tool_calls", "calls": [summary_call(seed)]},
        {"kind": "tool_calls", "calls": [summary_call(seed)]},
        DONE,
    ]
    ref = await run_agent(seed, script)
    outs = await rows(
        seed.tenant_id, InterviewCaptureOutput, InterviewCaptureOutput.capture_id == seed.capture_id
    )
    assert sorted(o.status for o in outs) == ["ready", "superseded"], ref


async def test_scorecard_submission_records_decisions(app: object, agent_org: object) -> None:
    from tests.agents.interview_assist.helpers import seed_interview

    seed = await seed_interview(app, agent_org.admin, agent_org.tenant, agent_org.candidate_id, in_past=True)  # type: ignore[attr-defined]
    try:
        await _ready(seed)
        await run_agent(seed, happy_script(seed))
        assert (
            await seed.ann.client.post(
                f"{API}/interviews/{seed.interview_id}/scorecard-draft/opened", headers=h(seed.ann)
            )
        ).status_code == 200
        body = {
            "ratings": [
                {"competency_key": "python", "rating": 4},
                {"competency_key": "communication", "rating": 5},
            ],
            "recommendation": "yes",
        }
        resp = await seed.ann.client.post(
            f"{API}/interviews/{seed.interview_id}/scorecards/mine/submit", json=body, headers=h(seed.ann)
        )
        assert resp.status_code == 200, resp.text
        decisions = await rows(seed.tenant_id, ScorecardSuggestionDecision)
        by_key = {d.competency_key: d.decision for d in decisions}
        assert by_key == {"python": "accepted", "communication": "changed"}
        resp = await seed.bob.client.post(
            f"{API}/interviews/{seed.interview_id}/scorecards/mine/submit", json=body, headers=h(seed.bob)
        )
        assert resp.status_code == 200, resp.text
        decisions = await rows(seed.tenant_id, ScorecardSuggestionDecision)
        assert sorted(d.decision for d in decisions) == ["accepted", "changed", "ignored", "ignored"]
        drafts = await rows(
            seed.tenant_id, InterviewScorecardDraft, InterviewScorecardDraft.interview_id == seed.interview_id
        )
        assert {d.status for d in drafts} == {"applied"}
    finally:
        await seed.ann.client.aclose()
        await seed.bob.client.aclose()


# ================================================================ 60.4 skills
async def test_interview_skill_acceptance_verified_only_for_interviewers(seed: Seed) -> None:
    from app.services import skills_service

    await _happy(seed)
    sugg = await rows(
        seed.tenant_id,
        SkillSuggestion,
        SkillSuggestion.source_ref == f"interview_capture:{seed.capture_id}",
        order_by=SkillSuggestion.created_at,
    )
    assert len(sugg) >= 2
    agent_ctx = RequestContext(
        tenant_id=seed.tenant_id,
        principal=_full_ctx(seed, seed.admin.user_id).principal,
        actor_kind="agent",
        actor_id=uuid.uuid4(),
    )
    with pytest.raises(Forbidden):
        async with tenant_session(seed.tenant_id) as db:
            await skills_service.accept_suggestion(agent_ctx, db, seed.candidate_id, sugg[0].id, None)
    async with tenant_session(seed.tenant_id) as db:
        await skills_service.accept_suggestion(
            _full_ctx(seed, seed.ann.user_id), db, seed.candidate_id, sugg[0].id, None
        )
    async with tenant_session(seed.tenant_id) as db:
        await skills_service.accept_suggestion(
            _full_ctx(seed, seed.admin.user_id), db, seed.candidate_id, sugg[1].id, None
        )
    ev = await rows(
        seed.tenant_id, CandidateSkillEvidence, CandidateSkillEvidence.candidate_id == seed.candidate_id
    )
    kinds = sorted(e.kind for e in ev if (e.source_ref or "").startswith("skill_suggestion:"))
    assert kinds == ["interview", "interview_declared"]


async def test_rejected_interview_suggestion_not_resuggested(seed: Seed) -> None:
    from app.services import skills_service

    await _happy(seed)
    sugg = await rows(
        seed.tenant_id, SkillSuggestion, SkillSuggestion.source_ref == f"interview_capture:{seed.capture_id}"
    )
    async with tenant_session(seed.tenant_id) as db:
        await skills_service.reject_suggestion(
            _full_ctx(seed, seed.ann.user_id), db, seed.candidate_id, sugg[0].id, "no"
        )
    async with tenant_session(seed.tenant_id) as db:
        capture = await db.get(InterviewCapture, seed.capture_id)
        from app.services.interview_captures import skill_evidence

        names = {sugg[0].skill_id}
        n, skipped = await skill_evidence.suggest_from_capture(
            RequestContext(tenant_id=seed.tenant_id),
            db,
            capture,
            [
                {
                    "skill_name": "Python",
                    "proficiency": 4,
                    "quote": "data pipeline in Python",
                    "segment_seq": 2,
                },
                {"skill_name": "Pandas", "proficiency": 3, "quote": "Pandas and Airflow", "segment_seq": 2},
            ],
        )  # type: ignore[arg-type]
    assert n == 0 and skipped and names


# ================================================================ A7 withdrawal / erasure
async def test_withdrawal_purges_recording_transcript_and_outputs(seed: Seed) -> None:
    from app.services.connectors.voice.fake import silence_wav
    from app.services.privacy import consent

    await consented(seed)
    from tests.agents.interview_assist.helpers import ctx_for

    ctx = await ctx_for(seed.tenant_id, seed.ann.user_id)
    async with tenant_session(seed.tenant_id) as db:
        await svc.upload_recording(
            ctx,
            db,
            seed.interview_id,
            filename="a.wav",
            data=silence_wav(),
            content_type="audio/wav",
            attest=True,
        )
    await upload(seed)
    await run_agent(seed, happy_script(seed))
    async with tenant_session(seed.tenant_id) as db:
        await consent.withdraw_consent(
            RequestContext(tenant_id=seed.tenant_id),
            db,
            seed.candidate_id,
            "interview_recording",
            by_kind="candidate",
        )
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.status == "erased" and capture.file_id is None and capture.recording_deleted_at is not None
    tr = await rows(seed.tenant_id, Transcript, Transcript.source_id == seed.capture_id)
    assert tr and all(t.status == "erased" for t in tr)
    outs = await rows(
        seed.tenant_id, InterviewCaptureOutput, InterviewCaptureOutput.capture_id == seed.capture_id
    )
    assert outs and all(o.status == "erased" and o.qa_items == [] for o in outs)
    drafts = await rows(
        seed.tenant_id, InterviewScorecardDraft, InterviewScorecardDraft.capture_id == seed.capture_id
    )
    assert all(d.status == "superseded" for d in drafts)
    sugg = await rows(
        seed.tenant_id, SkillSuggestion, SkillSuggestion.source_ref == f"interview_capture:{seed.capture_id}"
    )
    assert all(s.status == "rejected" for s in sugg)


async def test_erasure_handler(seed: Seed) -> None:
    from app.services.interview_captures_erasure import InterviewCaptureRecordsHandler
    from app.services.privacy.registry import ErasureContext

    await _happy(seed)
    handler = InterviewCaptureRecordsHandler()
    ectx = ErasureContext(
        tenant_id=seed.tenant_id, candidate_id=seed.candidate_id, run_id=uuid.uuid4(), reason="dsr"
    )
    async with tenant_session(seed.tenant_id) as db:
        exported = await handler.export(db, ectx)
        counts = await handler.erase(db, ectx)
        problems = await handler.verify(db, ectx)
    assert exported and counts["outputs"] == 1 and counts["drafts"] == 2 and problems == []
    capture = (await rows(seed.tenant_id, InterviewCapture, InterviewCapture.id == seed.capture_id))[0]
    assert capture.status == "erased"


async def test_interviews_get_other_interview_not_found(seed: Seed) -> None:
    await _ready(seed)
    script = [
        {"kind": "tool_calls", "calls": [["interviews.get", {"interview_id": str(uuid.uuid4())}]]},
        {"kind": "tool_calls", "calls": [["scorecards.submit", {"interview_id": str(seed.interview_id)}]]},
        DONE,
    ]
    ref = await run_agent(seed, script)
    calls = {
        c.tool_key: c for c in await rows(seed.tenant_id, AgentToolCall, AgentToolCall.run_id == ref.run_id)
    }
    assert calls["interviews.get"].outcome == "not_found"
    submit = calls.get("scorecards.submit") or calls["scorecards__submit"]
    assert submit.outcome == "policy_violation"


async def test_capture_ready_event_starts_the_run_for_the_uploader(seed: Seed) -> None:
    from app.services.ai.fake_llm import fake_llm
    from tests.agents.interview_assist.helpers import enable_assistant, turns

    await consented(seed)
    await enable_assistant(seed.tenant_id)
    with fake_llm(turns(DONE)):
        await upload(seed)
    runs = await rows(seed.tenant_id, AgentRun, AgentRun.agent_key == "interview_assist")
    assert len(runs) == 1, runs
    run = runs[0]
    assert run.trigger_key == "capture_ready" and run.owner_user_id == seed.ann.user_id
    assert run.subject_type == "interview" and run.subject_id == seed.interview_id
    assert run.dedup_key == f"ia:capture:{seed.capture_id}"
