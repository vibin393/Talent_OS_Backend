# ruff: noqa: E501
"""The ``billing`` AgentSpec (Epic 63 A6; FR-313).

Previews the invoice run of a Legal Entity and period, lists the exceptions (a note per client and kind) and PROPOSES
the same draft run a Finance User would start (``billing.prepare_run_draft``, high, suggest). On approval P2
``create_run`` runs as the approving Finance User: drafts only, no numbers. ★ never: issue, cancel, credit or send an
invoice, assign numbers, change lines or rates, record payments -- no tool in the allow-list reaches them.

Triggers: ``billing_day`` (kind ``schedule`` with a never-firing cron: ``collection_rules.billing_prep_tick`` starts the
runs on the 1st at 09:00 tenant-local, dedup ``{le}:{period}``; owner = the creator of the LE's last invoice run, none ->
a Task for the Tenant Admin), ``manual`` ("Prepare invoice run", owner = the starter).
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.billing.kickoff import kickoff

KEY = "billing"
NEVER = "0 0 31 2 *"
READ_TOOLS: tuple[str, ...] = ("billing.preview_run", "billing.check_rates", "billing.list_unapproved_time")
WRITE_TOOLS: tuple[str, ...] = ("billing.prepare_run_draft", "billing.flag_exception")
CORE_TOOLS: tuple[str, ...] = ("notify.user", "tasks.create")
TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS + CORE_TOOLS
PERMISSION_CEILING = dict.fromkeys(
    ("billing:read", "billing:write", "billing:prepare", "timesheet:read", "note:write", "task:create", "client:read"),
    Scope.TENANT,
)


class ClientSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    drafts: int = Field(ge=0)
    total: str = Field(max_length=40)
    currency: str | None = Field(default=None, max_length=3)
    exceptions: list[str] = Field(default_factory=list, max_length=12)


class BillingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "proposed", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=1500)
    period: str | None = Field(default=None, max_length=40)
    legal_entity_id: uuid.UUID | None = None
    clients: list[ClientSummary] = Field(default_factory=list, max_length=200)
    proposal_id: str | None = Field(default=None, max_length=40)


async def owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:
    from app.services import collection_rules_service

    return await collection_rules_service.billing_owner_for(ctx, db, subject)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:  # noqa: ARG001
    return "/billing/runs"


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(key="billing_day", kind="schedule", schedule=NEVER, on_active="drop", owner=owner),
    TriggerSpec(key="manual", kind="manual", on_active="attach"),
)

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="63", name="Billing Agent",
    purpose="Prepares the period's invoice run for a Finance User: previews drafts and totals, lists exceptions (missing PO, PO cap, rate outside card, unapproved time) and proposes the draft run. Never issues or numbers invoices.",
    prompt_id="agent_billing", model_tier="standard", effort="medium", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=8, max_tool_calls=16, max_writes=3, max_tokens_per_run=80_000, wall_clock_s=600, max_wait_hours=0),
    subject_type="legal_entity", origin="agent", triggers=TRIGGERS, owner=owner, kickoff=kickoff, result_model=BillingResult,
    fallback="owner_task", fallback_link=fallback_link, channels=("email", "in_app"), consent_purposes=(),
    eval_set="evals/billing", eval_threshold=0.9,
)  # fmt: skip

__all__ = ["CORE_TOOLS", "KEY", "PERMISSION_CEILING", "READ_TOOLS", "SPEC", "TOOLS", "TRIGGERS", "WRITE_TOOLS", "BillingResult"]
