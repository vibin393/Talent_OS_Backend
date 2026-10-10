# ruff: noqa: E501
"""The ``timesheet`` AgentSpec (Epic 63 A6; FR-311 / FR-312).

Nudge runs (subject ``user`` = the Account Manager, owner = the AM): read the plan, send ONE templated reminder call
through P2 ``remind`` and escalate long-pending approvals with a Task. Flag runs (subject ``timesheet``, owner = the
Placement AM): explain the deterministic flags the rules engine raised. ★ never: approve / reject / reopen a timesheet,
edit hours or entries, add / remove flags -- no tool in the allow-list reaches them.

Triggers: ``daily_nudges`` (kind ``schedule`` with a never-firing cron: ``timesheet_flags.nudge_tick`` starts the runs
at 10:00 tenant-local), ``flags_raised`` (kind ``inbound``: the ``timesheet_flags`` hooks start it on
``TimesheetFlagsRaised``, dedup ``flags:{timesheet}:{version}``), ``manual`` ("Nudge now", subject = me), ``workflow``.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.timesheet.kickoff import kickoff

KEY = "timesheet"
NEVER = "0 0 31 2 *"  # 31 February: the runtime scheduler never fires it; timesheet_flags.nudge_tick starts the runs
READ_TOOLS: tuple[str, ...] = ("time.get_nudge_plan", "time.get_timesheet", "time.get_context", "time.list_flags")
WRITE_TOOLS: tuple[str, ...] = ("time.send_timesheet_reminder", "time.save_flag_explanations", "time.escalate_approval")
CORE_TOOLS: tuple[str, ...] = ("notify.user", "tasks.create")
TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS + CORE_TOOLS
PERMISSION_CEILING = dict.fromkeys(
    ("timesheet:read", "timesheet:monitor", "placement:read", "message:send_templated", "task:create"), Scope.TENANT
)


class TimesheetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=1500)
    nudges: int = Field(default=0, ge=0, le=200)
    escalations: int = Field(default=0, ge=0, le=200)
    flags_explained: int = Field(default=0, ge=0, le=50)


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import timesheet_flags_service

    return await timesheet_flags_service.timesheet_owner(ctx, db, subject)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    if subject is not None and subject[0] == "timesheet":
        return f"/timesheets/{subject[1]}"
    return "/timesheets?view=monitor"


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(key="daily_nudges", kind="schedule", schedule=NEVER, on_active="drop", owner=owner),
    TriggerSpec(key="flags_raised", kind="inbound", on_active="drop", owner=owner),
    TriggerSpec(key="manual", kind="manual", on_active="attach", owner=owner),
    TriggerSpec(key="workflow", kind="workflow", owner=owner),
)

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="63", name="Timesheet Agent",
    purpose="Nudges Workers and approvers about late timesheets with the standard reminder, escalates long-pending approvals to the Account Manager, and explains anomaly flags. Never approves, rejects or edits time.",
    prompt_id="agent_timesheet", model_tier="standard", effort="low", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=6, max_tool_calls=20, max_writes=6, max_tokens_per_run=40_000, wall_clock_s=300, max_wait_hours=0),
    subject_type=None, origin="agent", triggers=TRIGGERS, owner=owner, kickoff=kickoff, result_model=TimesheetResult,
    fallback="owner_task", fallback_link=fallback_link, channels=("email",), consent_purposes=(),
    eval_set="evals/timesheet", eval_threshold=0.9,
)  # fmt: skip

__all__ = ["CORE_TOOLS", "KEY", "PERMISSION_CEILING", "READ_TOOLS", "SPEC", "TOOLS", "TRIGGERS", "WRITE_TOOLS", "TimesheetResult"]
