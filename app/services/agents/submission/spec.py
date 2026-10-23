# ruff: file-ignore[line-too-long, non-imperative-mood]
"""The ``submission`` AgentSpec (Epic 61, spec A4; FR-305..FR-307).

Prepares an internal draft submission pack for a shortlisted Application: ``submissions.prepare_pack`` (deterministic:
resume, rates, checks), then the write-up through ``submissions.save_write_up`` (validated in code), the templated
consent request when consent is missing, and a note listing blockers. ★ never: creating or sending a Submission
(``("submission", "sent")``), moving the Application, approving a margin override / rate exception, changing rates on a
Candidate or Offer -- no tool in the allow-list reaches them.

Triggers: ``shortlisted`` (kind ``inbound``; the ``submission_packs`` hooks start it on ``ApplicationStageChanged`` to
``shortlisted`` by a person, dedup ``shortlisted:<application>``), ``manual`` ("Prepare submission", ``attach`` to an
active run) and ``workflow`` ("Start agent run").
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.submission import kickoff

KEY = "submission"
T = Scope.TENANT

READ_TOOLS: tuple[str, ...] = ("submissions.get_pack_context", "submissions.check_pack")
WRITE_TOOLS: tuple[str, ...] = (
    "submissions.prepare_pack",
    "submissions.save_write_up",
    "submissions.request_consent",
)
CORE_TOOLS: tuple[str, ...] = ("consent.status", "tasks.create", "notes.add", "flags.raise", "notify.user")
TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS + CORE_TOOLS

PERMISSION_CEILING = dict.fromkeys(
    (
        "application:read", "candidate:read", "demand:read", "submission:read", "submission:draft", "consent:write",
        "message:send_templated", "compensation:view", "note:write", "task:create",
    ),
    T,
)  # fmt: skip


class SubmissionResult(BaseModel):
    """``emit_result`` schema: ids and labels only."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=600)
    pack_id: uuid.UUID | None = None
    blockers: list[str] = Field(default_factory=list, max_length=10)


async def application_owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    """The Application owner, else the Demand owner; none -> no run (FR-265)."""
    from app.services import submission_packs_service

    return await submission_packs_service.application_owner(ctx, db, subject)


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(key="shortlisted", kind="inbound", on_active="drop", owner=application_owner),
    TriggerSpec(key="manual", kind="manual", on_active="attach"),
    TriggerSpec(key="workflow", kind="workflow"),
)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return (
        f"/pipeline/applications/{subject[1]}?panel=submission-pack" if subject is not None else "/pipeline"
    )


SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="61",
    name="Submission Agent",
    purpose="Prepares a draft client submission pack (resume, write-up, rates, pre-send checks) for a person to review and send. Never sends.",
    prompt_id="agent_submission",
    model_tier="standard",
    effort="medium",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=8, max_tool_calls=16, max_writes=4, max_tokens_per_run=80_000, wall_clock_s=300, max_wait_hours=0),
    subject_type="application",
    origin="agent",
    triggers=TRIGGERS,
    owner=application_owner,
    kickoff=kickoff.build,
    result_model=SubmissionResult,
    fallback="owner_task",
    fallback_link=fallback_link,
    channels=("email",),
    consent_purposes=("ai_processing",),
    aedt_feature=None,
    eval_set="evals/submission",
    eval_threshold=0.9,
)  # fmt: skip

__all__ = [
    "CORE_TOOLS",
    "KEY",
    "PERMISSION_CEILING",
    "READ_TOOLS",
    "SPEC",
    "TOOLS",
    "TRIGGERS",
    "WRITE_TOOLS",
    "SubmissionResult",
    "application_owner",
]
