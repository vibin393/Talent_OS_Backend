# ruff: noqa: E501
"""The ``interview_assist`` AgentSpec (Epic 60, spec A5; FR-301..FR-304).

* Subject: the Interview; Accountable Owner: the uploader of the capture (explicit for manual re-runs).
* Effective Rights = ``permission_ceiling`` ∩ the uploader's own rights.
* Triggers: ``capture_ready`` (``InterviewCaptureReady``, dedup ``ia:capture:<capture_id>``) and ``manual``
  ("Re-run assistant").
* Write tools are ``low`` with default ``auto`` after burn-in: they only write private, reviewable drafts, outputs
  and P3 skill suggestions. During burn-in they are Proposals to the uploader.
* ★ never: scorecard submission, recommendation, interview outcome, stage moves, verifying skill evidence -- no tool
  reaches them. Fallback: an owner Task linking the blank scorecard.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services import interview_captures_service
from app.services.agents.interview_assist.kickoff import kickoff

KEY = "interview_assist"
T = Scope.TENANT

ASSIST_TOOLS: tuple[str, ...] = (
    "interviews.get",
    "interview_kits.get",
    "interview_capture.policy_status",
    "interview_capture.transcript",
    "interview_capture.transcribe",
    "interview_capture.save_summary",
    "scorecards.create_draft",
    "skills.suggest_evidence",
)
CORE_TOOLS: tuple[str, ...] = ("tasks.create", "notify.user", "consent.status")
TOOLS = ASSIST_TOOLS + CORE_TOOLS

PERMISSION_CEILING = {
    "interview:read": T,
    "interview_capture:manage": T,
    "scorecard:submit": Scope.OWN,  # reading kits / draft targets only -- there is no submit tool
    "candidate:read": T,
    "task:create": T,
}


class AssistantResult(BaseModel):
    """``emit_result`` schema: counts only."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)
    drafts: int = Field(default=0, ge=0, le=50)
    skill_suggestions: int = Field(default=0, ge=0, le=50)


def _interview(e: object) -> tuple[str, uuid.UUID]:
    return ("interview", e.interview_id)  # type: ignore[attr-defined]


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/interviews/{subject[1]}/scorecard" if subject is not None else "/interviews"


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(
        key="capture_ready", kind="event", event="InterviewCaptureReady", event_module="app.events.interview_captures",
        subject=_interview, dedup_key=lambda e: f"ia:capture:{e.capture_id}", on_active="drop",
        owner=interview_captures_service.capture_owner,
    ),
    TriggerSpec(key="manual", kind="manual"),
)  # fmt: skip

SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="60",
    name="Interview Assistant",
    purpose=(
        "Turns a consented interview transcript into a summary, questions and answers, skill suggestions and a draft "
        "scorecard for the interviewer."
    ),
    prompt_id="agent_interview_assist",
    model_tier="standard",
    effort="medium",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="auto",
    limits=RunLimits(max_steps=12, max_tool_calls=30, max_writes=10, max_wait_hours=0),
    subject_type="interview",
    origin="agent",
    triggers=TRIGGERS,
    owner=interview_captures_service.capture_owner,
    kickoff=kickoff,
    result_model=AssistantResult,
    fallback="owner_task",
    fallback_link=fallback_link,
    channels=(),
    consent_purposes=("ai_processing", "interview_recording"),
    aedt_feature="agent_interview_assist",
    eval_set="evals/interview_assistant",
    eval_threshold=0.9,
)

__all__ = ["KEY", "PERMISSION_CEILING", "SPEC", "TOOLS", "TRIGGERS", "AssistantResult"]
