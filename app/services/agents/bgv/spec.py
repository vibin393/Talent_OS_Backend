# ruff: noqa: E501
"""The ``bgv`` AgentSpec (Epic 62 A6, FR-309). Owner = the BGV case owner. Triggers: ``check_overdue`` (P2
``BgvCheckOverdue``), ``daily_follow_up`` (started by ``chasing.bgv_tick``), ``discrepancy`` (``BgvDiscrepancyRaised`` ->
summary only), ``manual``. ★ never: adjudicate, waive, record results, send candidate documents."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.bgv.kickoff import kickoff

KEY = "bgv"
NEVER = "0 0 31 2 *"
TOOLS: tuple[str, ...] = (
    "bgv.get_case_summary", "bgv.list_overdue_checks", "bgv.request_provider_status", "bgv.send_provider_follow_up",
    "bgv.draft_provider_message", "bgv.request_candidate_documents", "bgv.post_case_summary", "tasks.create", "notify.user",
)  # fmt: skip
PERMISSION_CEILING = dict.fromkeys(
    ("bgv:read", "bgv:manage", "bgv:follow_up", "email:send", "note:write", "task:create"), Scope.TENANT
)


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import chasing_service

    return await chasing_service.bgv_owner(ctx, db, subject)


class BgvResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)
    follow_ups: int = Field(default=0, ge=0, le=50)
    escalated: bool = False


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/bgv/cases/{subject[1]}" if subject else "/bgv"


TRIGGERS = (
    TriggerSpec(
        key="check_overdue", kind="event", event="BgvCheckOverdue", event_module="app.services.bgv.events",
        subject=lambda e: ("bgv_case", e.case_id), dedup_key=lambda e: f"overdue:{e.check_id}", on_active="drop", owner=owner,
    ),
    TriggerSpec(key="daily_follow_up", kind="schedule", schedule=NEVER, on_active="drop", owner=owner),
    TriggerSpec(
        key="discrepancy", kind="event", event="BgvDiscrepancyRaised", event_module="app.events.bgv",
        subject=lambda e: ("bgv_case", e.case_id), dedup_key=lambda e: f"discrepancy:{e.discrepancy_id}", on_active="drop",
        owner=owner,
    ),
    TriggerSpec(key="manual", kind="manual"),
)  # fmt: skip

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="62", name="BGV Agent",
    purpose="Follows up background-verification providers about overdue checks and summarises pending items for the case owner.",
    prompt_id="agent_bgv", model_tier="standard", effort="low", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=8, max_tool_calls=16, max_writes=6, max_tokens_per_run=40_000, wall_clock_s=300, max_wait_hours=0),
    subject_type="bgv_case", origin="agent", triggers=TRIGGERS, owner=owner, kickoff=kickoff, result_model=BgvResult,
    fallback="owner_task", fallback_link=fallback_link, channels=("email",), consent_purposes=("ai_processing",),
    eval_set="evals/bgv", eval_threshold=0.9,
)  # fmt: skip

__all__ = ["KEY", "PERMISSION_CEILING", "SPEC", "TOOLS", "BgvResult"]
