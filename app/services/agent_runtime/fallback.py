# ruff: file-ignore[line-too-long]
"""Manual fallback (Epic 53, story 53.8, FR-275): when a run fails, stops or is blocked, the Accountable Owner gets a
Task linking the manual screen (or the run). Idempotent per run (``agent_runs.fallback_task_id``)."""

import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import AgentSpec
from app.core.context import RequestContext
from app.models.agent_runtime import AgentRun
from app.models.tasks import TASK_ENTITY_TYPES
from app.schemas.tasks import TaskCreate
from app.services import tasks_service

REASON_LABELS = {
    "cap_steps": "step limit reached",
    "cap_tool_calls": "tool-call limit reached",
    "cap_tokens": "token limit reached",
    "budget": "AI budget reached",
    "kill_switch": "agents were stopped",
    "provider_error": "the AI service was unavailable",
    "policy_violation": "an action was blocked by policy",
    "consent_missing": "consent is missing",
    "stale_data": "the record changed",
    "owner_unavailable": "the owner is unavailable",
    "timeout": "it ran out of time",
    "refused": "the AI declined the task",
    "subject_closed": "the record was closed",
    "manual": "it was cancelled",
    "needs_human": "it needs a person",
}


async def create_task(
    db: AsyncSession, run: AgentRun, spec: AgentSpec, reason: str, *, owner_user_id: uuid.UUID | None = None
) -> uuid.UUID | None:
    """Create the fallback Task (once per run); returns its id or None when no task could be created."""
    if run.fallback_task_id is not None or spec.fallback == "none":
        return run.fallback_task_id
    subject = (run.subject_type, run.subject_id) if run.subject_type and run.subject_id else None
    link = spec.fallback_link(subject) if spec.fallback_link is not None else f"/ai/runs/{run.id}"
    if subject is not None and subject[0] in TASK_ENTITY_TYPES:
        entity_type, entity_id = subject
    else:
        entity_type, entity_id = "agent_run", run.id
    label = REASON_LABELS.get(reason, reason)
    data = TaskCreate.model_construct(
        title=f"{spec.name} could not finish: {label}"[:200],
        notes=f"AI agent run {run.id} ({spec.name}) stopped: {label}. Continue manually: {link}",
        owner_id=owner_user_id or run.owner_user_id,
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=entity_id,
        due_at=datetime.now(UTC) + timedelta(hours=24),
        due_all_day=False,
        priority="high",
        reminder_at=None,
    )
    ctx = RequestContext(tenant_id=run.tenant_id, agent_run_id=run.id, agent_key=run.agent_key)
    try:
        async with db.begin_nested():
            task = await tasks_service.create_system_task(ctx, db, data, source="agent")
    except Exception:
        logger.exception("Could not create the fallback task of agent run {}", run.id)
        return None
    run.fallback_task_id = task.id
    return task.id


__all__ = ["REASON_LABELS", "create_task"]
