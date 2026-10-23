# ruff: file-ignore[line-too-long]
"""The ``onboarding`` AgentSpec (Epic 62 A6, FR-310). A conversation agent (queue ``conversations``): a Worker's question
starts or resumes a run on the thread; replies go out only as Grounded Answers. Owner = the thread's coordinator.
``fallback="none"``: a wait that ends at the wall clock is normal; a failed run hands the question to the coordinator
(``knowledge`` hooks on ``AgentRunFailed``)."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.onboarding.kickoff import kickoff

KEY = "onboarding"
TOOLS: tuple[str, ...] = (
    "onboarding.get_thread", "onboarding.get_my_case", "knowledge.search", "documents.list_my_signature_requests",
    "onboarding.reply_grounded", "onboarding.hand_off",
)  # fmt: skip
# ``candidate:read`` only lets the send policy confirm the asker is visible to the coordinator (no tool reads candidates).
PERMISSION_CEILING = dict.fromkeys(
    ("onboarding:read", "knowledge:read", "task:create", "document:read", "candidate:read"),
    Scope.TENANT,
)


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import knowledge_service

    return await knowledge_service.thread_owner(ctx, db, subject)


class OnboardingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/onboarding/questions?thread={subject[1]}" if subject else "/onboarding/questions"


TRIGGERS = (TriggerSpec(key="question", kind="inbound", on_active="attach", owner=owner),)

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="62", name="Onboarding Agent",
    purpose="Answers a Worker's or new joiner's onboarding questions from published knowledge sources and their own tasks, with citations; hands off pay, tax, legal and immigration questions.",
    prompt_id="agent_onboarding", model_tier="standard", effort="medium", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=6, max_tool_calls=10, max_writes=2, max_tokens_per_run=40_000, max_wait_hours=72),
    subject_type="onboarding_qa_thread", origin="conversation", triggers=TRIGGERS, owner=owner, kickoff=kickoff,
    result_model=OnboardingResult, fallback="none", fallback_link=fallback_link, channels=("portal",),
    consent_purposes=("ai_processing", "knowledge_assistant"), eval_set="evals/onboarding", eval_threshold=0.9,
)  # fmt: skip

__all__ = ["KEY", "PERMISSION_CEILING", "SPEC", "TOOLS", "OnboardingResult"]
