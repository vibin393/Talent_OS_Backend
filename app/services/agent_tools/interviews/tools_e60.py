# ruff: file-ignore[line-too-long]
"""Interview Assistant tools of the ``interviews`` domain (Epic 60 Part B; ``owner_epic="60"``, ``version=1``).

| Key | Risk | Default after burn-in |
|---|---|---|
| ``interviews.get`` / ``interview_kits.get`` | read | auto |
| ``scorecards.create_draft`` | low | auto (a private draft for one interviewer; never the P1 scorecard) |

``interviews.get`` is registered here because Epic 54 registered only ``interviews.list`` (A0). There is no
``recommendation`` field anywhere and no submit tool: scorecard submission, recommendations, interview outcomes and stage
moves are ★ (absent from the registry). The handlers call only the ``interview_captures_service`` facade (which reads
P1 interviews through its own read functions).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.core.agentic import IdempotencyRule, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.interview_capture.common_e60 import (
    OWNER_EPIC,
    VERSION,
    In,
    Out,
    preview,
    subject,
)

INTERVIEW_SCOPE: dict[str, Any] = {"resource": "interview", "target_arg": "interview_id"}


# ================================================================ interviews.get (read)
class InterviewIn(In):
    interview_id: uuid.UUID


class InterviewerOut(Out):
    participant_id: uuid.UUID
    user_id: uuid.UUID | None
    name: str | None
    scorecard_required: bool


class InterviewGetOut(Out):
    stage_name: str
    mode: str
    starts_at: datetime
    ends_at: datetime
    status: str
    candidate_id: uuid.UUID
    capture_id: uuid.UUID | None
    capture_status: str | None
    interviewer_participants: list[InterviewerOut]


async def _get(tctx: Any, args: InterviewIn) -> ToolOutcome:
    from app.services import interview_captures_service as svc

    data = await svc.agent_interview(tctx.db, subject(tctx), args.interview_id)
    i, capture = data["interview"], data["capture"]
    out = InterviewGetOut(
        stage_name=i.stage_name, mode=i.mode, starts_at=i.starts_at, ends_at=i.ends_at, status=i.status,
        candidate_id=i.candidate_id, capture_id=capture.id if capture else None,
        capture_status=capture.status if capture else None,
        interviewer_participants=[InterviewerOut(**p) for p in data["interviewers"]],
    )  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("interview", i.id)]
    if capture is not None:
        refs.append(("interview_capture", capture.id))
    return ToolOutcome(
        out,
        refs=tuple(refs),
        candidate_ids=(i.candidate_id,),
        summary=f"{len(out.interviewer_participants)} interviewer(s)",
    )


register_tool(
    ToolSpec(
        key="interviews.get", version=VERSION, owner_epic=OWNER_EPIC, title="Read the interview",
        description="The run's interview: stage, mode, times, status, the candidate id, the capture id and status, and the internal interviewer participants (participant id, name, whether they must submit a scorecard). No emails.",
        input_model=InterviewIn, output_model=InterviewGetOut, permissions=("interview:read",), risk="read",
        scope=ScopeRule(**INTERVIEW_SCOPE), handler=_get,
    )
)  # fmt: skip


# ================================================================ interview_kits.get (read)
class CompetencyOut(Out):
    key: str
    label: str
    guidance: str | None


class KitQuestionOut(Out):
    key: str
    competency_key: str | None
    text: str
    guidance: str | None


class KitOut(Out):
    competencies: list[CompetencyOut]
    questions: list[KitQuestionOut]
    rating_scale: int


async def _kit(tctx: Any, args: InterviewIn) -> ToolOutcome:
    from app.services import interview_captures_service as svc

    kit = await svc.agent_kit(tctx.db, subject(tctx), args.interview_id)
    out = KitOut(
        competencies=[CompetencyOut(**c) for c in kit.competencies],
        questions=[KitQuestionOut(**q) for q in kit.questions], rating_scale=kit.rating_scale,
    )  # fmt: skip
    return ToolOutcome(
        out,
        refs=(("interview", args.interview_id),),
        summary=f"{len(out.competencies)} competencies, {len(out.questions)} questions",
    )


register_tool(
    ToolSpec(
        key="interview_kits.get", version=VERSION, owner_epic=OWNER_EPIC, title="Read the interview kit",
        description="The interview's kit snapshot: competencies (key, label, guidance), questions (key, competency key, text) and the rating scale (1..rating_scale). Without a kit there is one competency 'overall'.",
        input_model=InterviewIn, output_model=KitOut, permissions=("interview:read",), risk="read",
        scope=ScopeRule(**INTERVIEW_SCOPE), handler=_kit,
    )
)  # fmt: skip


# ================================================================ scorecards.create_draft (low)
class QuoteIn(In):
    segment_seq: int = Field(ge=1)
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class RatingIn(In):
    competency_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    suggested_rating: int = Field(ge=1, le=10)
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    evidence: list[QuoteIn] = Field(min_length=1, max_length=5)


class AnswerIn(In):
    question_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    note_suggestion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class CreateDraftIn(In):
    capture_id: uuid.UUID
    participant_id: uuid.UUID
    ratings: list[RatingIn] = Field(min_length=1, max_length=30)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=60)


class CreateDraftOut(Out):
    draft_id: uuid.UUID


async def _create_draft(tctx: Any, args: CreateDraftIn) -> ToolOutcome:
    from app.services import interview_captures_service as svc

    data = args.model_dump(mode="python")
    draft_id, candidate_id = await svc.agent_create_draft(
        tctx.db,
        subject(tctx),
        tctx.run_id,
        args.capture_id,
        args.participant_id,
        data["ratings"],
        data["answers"],
    )
    return ToolOutcome(
        CreateDraftOut(draft_id=draft_id), refs=(("interview_capture", args.capture_id),), candidate_ids=(candidate_id,),
        target=("interview_capture", args.capture_id), summary=f"draft with {len(args.ratings)} suggested ratings",
    )  # fmt: skip


register_tool(
    ToolSpec(
        key="scorecards.create_draft", version=VERSION, owner_epic=OWNER_EPIC, title="Prepared a draft scorecard",
        description=(
            "Prepare one interviewer's private draft scorecard (participant_id of an internal interviewer who must submit a "
            "scorecard): one suggested rating per Kit competency within the rating scale, each with a short rationale and at "
            "least one exact transcript quote; optional note suggestions per Kit question. There is no recommendation -- the "
            "interviewer decides it. The interviewer reviews, edits and submits their own scorecard; nothing is submitted. "
            "Never mention tone, voice, emotion, personality, honesty or personal traits (summary_language)."
        ),
        input_model=CreateDraftIn, output_model=CreateDraftOut, permissions=("interview_capture:manage",), risk="low",
        scope=ScopeRule(resource="interview_capture", target_arg="capture_id", cross_subject=True), handler=_create_draft,
        default_mode="auto",
        preview=preview("Prepare a draft scorecard", lambda a: f"Suggested ratings for {len(a.ratings)} competencies (no recommendation) for interviewer {a.participant_id}"),
        untrusted_input=frozenset({"ratings", "answers"}),
        idempotency=IdempotencyRule(fields=("capture_id", "participant_id")), max_calls_per_run=10,
    )
)  # fmt: skip
