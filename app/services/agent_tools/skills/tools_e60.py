# ruff: noqa: E501
"""Interview Assistant tool of the ``skills`` domain (Epic 60 Part B; ``owner_epic="60"``, ``version=1``).

``skills.suggest_evidence`` (low, default auto after burn-in): P3 skill suggestions of source ``interview`` (C-10) with
the quote as rationale -- never evidence on the profile. A person accepts or rejects each suggestion on the Candidate's
Skills tab; only an interviewer on that Interview accepting records *verified* evidence. There is no tool to verify or
accept a skill (★).
"""

import uuid
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.core.agentic import IdempotencyRule, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.interview_capture.common_e60 import (
    OWNER_EPIC,
    VERSION,
    In,
    Out,
    preview,
    scoped,
    subject,
)


class EvidenceItemIn(In):
    skill_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    proficiency: int = Field(ge=1, le=5)
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    segment_seq: int = Field(ge=1)


class SuggestEvidenceIn(In):
    capture_id: uuid.UUID
    items: list[EvidenceItemIn] = Field(min_length=1, max_length=20)


class SkippedOut(Out):
    skill_name: str
    reason: str


class SuggestEvidenceOut(Out):
    suggested: int
    skipped: list[SkippedOut]


async def _suggest(tctx: Any, args: SuggestEvidenceIn) -> ToolOutcome:
    from app.services import interview_captures_service as svc

    items = [i.model_dump() for i in args.items]
    suggested, skipped, candidate_id = await svc.agent_suggest_evidence(
        scoped(tctx), tctx.db, subject(tctx), args.capture_id, items
    )
    return ToolOutcome(
        SuggestEvidenceOut(suggested=suggested, skipped=[SkippedOut(**s) for s in skipped]),
        refs=(("interview_capture", args.capture_id), ("candidate", candidate_id)), candidate_ids=(candidate_id,),
        target=("interview_capture", args.capture_id), summary=f"{suggested} skill suggestion(s)",
    )  # fmt: skip


register_tool(
    ToolSpec(
        key="skills.suggest_evidence", version=VERSION, owner_epic=OWNER_EPIC, title="Suggested skills from the interview",
        description=(
            "Suggest up to 20 skills the candidate demonstrated in the interview: skill name (catalogue names only; unknown "
            "names are skipped), proposed proficiency 1-5 and an exact quote with its segment number. They become suggestions "
            "on the candidate's Skills tab that a person accepts or rejects; you cannot verify a skill. Skills the candidate "
            "already has or that were rejected before are skipped."
        ),
        input_model=SuggestEvidenceIn, output_model=SuggestEvidenceOut, permissions=("interview_capture:manage", "candidate:read"),
        risk="low", scope=ScopeRule(resource="interview_capture", target_arg="capture_id", cross_subject=True), handler=_suggest,
        default_mode="auto",
        preview=preview("Suggest skills from the interview", lambda a: f"{len(a.items)} skill suggestion(s): " + ", ".join(i.skill_name for i in a.items[:10])),
        untrusted_input=frozenset({"items"}), idempotency=IdempotencyRule(fields=("capture_id",)), max_calls_per_run=2,
    )
)  # fmt: skip
