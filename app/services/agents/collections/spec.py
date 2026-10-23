# ruff: file-ignore[line-too-long]
"""The ``collections`` AgentSpec (Epic 63 A6; FR-314 / FR-315).

Applies the Client's collection rule to open invoices: templated pre-due reminders (``low``, default ``suggest``: auto
only when the tenant opts in after burn-in, the step allows it and the run is untainted); every overdue reminder,
escalation, custom message and statement is a Proposal (``high``) to the collections owner. Disputed, do-not-remind,
paused and taken-over documents are excluded. ★ never: record / reverse payments, write off, resolve disputes, close
periods, promise settlements -- no tool in the allow-list reaches them.

Owner = ``client_collection_settings.owner_id`` -> the Client owner -> none (no run + a Task for the Tenant Admin).
Triggers: ``daily_scan`` (kind ``schedule`` with a never-firing cron: ``collection_rules.daily_tick`` starts the runs at
10:30 tenant-local), ``ptp_broken`` (kind ``inbound``: the domain hooks start it on ``PromiseToPayBroken``, dedup
``ptp:{promise}``), ``manual`` ("Run collections now"; the only runs that may read activity bodies), ``workflow``.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.collections.kickoff import kickoff

KEY = "collections"
NEVER = "0 0 31 2 *"
READ_TOOLS: tuple[str, ...] = (
    "receivables.get_collection_plan", "receivables.get_client_context", "receivables.get_activity_bodies",
)  # fmt: skip
WRITE_TOOLS: tuple[str, ...] = (
    "receivables.send_pre_due_reminder", "receivables.send_reminder", "receivables.send_dunning_message",
    "receivables.send_statement", "receivables.log_collection_note",
)  # fmt: skip
CORE_TOOLS: tuple[str, ...] = ("tasks.create", "notify.user")
TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS + CORE_TOOLS
PERMISSION_CEILING = dict.fromkeys(
    (
        "receivables:read",
        "collections:write",
        "email:send",
        "message:send_templated",
        "task:create",
        "client:read",
        "contact:read",
    ),
    Scope.TENANT,
)


class Skip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    reason: str = Field(max_length=60)


class CollectionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "proposed", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=1500)
    sent: int = Field(default=0, ge=0, le=50)
    proposals: int = Field(default=0, ge=0, le=50)
    skipped: list[Skip] = Field(default_factory=list, max_length=50)


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import collection_rules_service

    return await collection_rules_service.collections_owner_for(ctx, db, subject)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return (
        f"/receivables/collections?client={subject[1]}" if subject is not None else "/receivables/collections"
    )


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(key="daily_scan", kind="schedule", schedule=NEVER, on_active="drop", owner=owner),
    TriggerSpec(key="ptp_broken", kind="inbound", on_active="drop", owner=owner),
    TriggerSpec(key="manual", kind="manual", on_active="attach"),
    TriggerSpec(key="workflow", kind="workflow", owner=owner),
)

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="63", name="Collections Agent",
    purpose="Follows each client's collection rule: standard pre-due reminders, and overdue reminders, escalations and statements drafted for the collections owner's approval. Never records payments, writes off or offers settlements.",
    prompt_id="agent_collections", model_tier="standard", effort="medium", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=8, max_tool_calls=16, max_writes=4, max_tokens_per_run=60_000, wall_clock_s=300, max_wait_hours=0),
    subject_type="client", origin="agent", triggers=TRIGGERS, owner=owner, kickoff=kickoff, result_model=CollectionsResult,
    fallback="owner_task", fallback_link=fallback_link, channels=("email",), consent_purposes=(),
    eval_set="evals/collections", eval_threshold=0.9,
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
    "CollectionsResult",
]
