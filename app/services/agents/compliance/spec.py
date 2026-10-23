# ruff: file-ignore[line-too-long]
"""The ``compliance`` AgentSpec (Epic 62 A6, FR-308).

Owner = the Placement's onboarding owner -> ``onboarding_settings.default_hr_owner_id`` -> an ``hr_onboarding`` user ->
none (no run + a Task to the Tenant Admin, FR-265). Runs start deterministically (``chasing.compliance_tick`` /
``placement_created``), one per Worker with something due. ★ never: waive, verify / reject, start-gate override,
Joined, editing Worker data -- no tool in the allow-list reaches them.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.compliance.kickoff import kickoff

KEY = "compliance"
NEVER = (
    "0 0 31 2 *"  # 31 February: the runtime scheduler never fires it; chasing.compliance_tick starts the runs
)
TOOLS: tuple[str, ...] = (
    "compliance.get_chase_plan", "compliance.send_chase_reminder", "compliance.escalate_chase", "compliance.get_risk_list",
    "consent.status", "messaging.create_draft", "messaging.send_templated", "tasks.create", "notes.add", "flags.raise",
    "notify.user",
)  # fmt: skip
PERMISSION_CEILING = dict.fromkeys(
    (
        "compliance:read", "placement:read", "candidate:read", "message:draft", "message:send_templated", "email:send",
        "messaging:send", "note:write", "task:create",
    ),
    Scope.TENANT,
)  # fmt: skip


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import chasing_service

    return await chasing_service.compliance_owner(ctx, db, subject)


class ComplianceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)
    reminders_sent: int = Field(default=0, ge=0, le=20)
    escalated: bool = False


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/compliance/pre-start-risk?candidate={subject[1]}" if subject else "/compliance/pre-start-risk"


TRIGGERS = (
    TriggerSpec(key="daily_scan", kind="schedule", schedule=NEVER, on_active="drop", owner=owner),
    TriggerSpec(key="placement_created", kind="inbound", on_active="drop", owner=owner),
    TriggerSpec(key="manual", kind="manual"),
    TriggerSpec(key="workflow", kind="workflow"),
)

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="62", name="Compliance Agent",
    purpose="Chases Workers for missing compliance documents and expiring credentials with approved reminder templates, and escalates to the coordinator.",
    prompt_id="agent_compliance", model_tier="standard", effort="low", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=6, max_tool_calls=12, max_writes=4, max_tokens_per_run=30_000, wall_clock_s=300, max_wait_hours=0),
    subject_type="candidate", origin="agent", triggers=TRIGGERS, owner=owner, kickoff=kickoff, result_model=ComplianceResult,
    fallback="owner_task", fallback_link=fallback_link, channels=("portal", "email", "sms", "whatsapp"),
    consent_purposes=("ai_processing",), eval_set="evals/compliance", eval_threshold=0.9,
)  # fmt: skip

__all__ = ["KEY", "PERMISSION_CEILING", "SPEC", "TOOLS", "ComplianceResult"]
