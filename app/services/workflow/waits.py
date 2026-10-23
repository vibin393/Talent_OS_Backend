# ruff: file-ignore[unused-function-argument, line-too-long]
"""Workflow waits (Epic 32, story 19.3): delay / approval parking of graph runs.

* Approval decisions: subject ``workflow_step`` (``register_approval_subject``). ``on_decided`` runs inside the
  decision transaction (AD-24) and only marks the wait ``resumed`` with the outcome and emits
  ``WorkflowWaitResumed``; its handler enqueues ``workflow.continue_run`` after the commit, so the decision
  transaction never runs actions.
* Beat ``workflow.resume_waits`` (every minute, per tenant, ``SKIP LOCKED``): due delays resume; approval waits
  past ``timeout_at`` cancel their request (system) and take the ``on_timeout`` branch.
* Deactivating / archiving a rule cancels its waiting rows and their open approval requests.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext, reset_context, set_context
from app.core.events import emit
from app.core.settings import get_settings
from app.events.workflow import WorkflowWaitResumed
from app.models.workflow import WorkflowExecution, WorkflowRule, WorkflowWait
from app.schemas.approvals import SubjectSummary

SUBJECT = "workflow_step"


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ approval subject
async def on_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    wait = (
        await db.execute(select(WorkflowWait).where(WorkflowWait.id == decision.subject_id).with_for_update())
    ).scalar_one_or_none()
    if wait is None or wait.status != "waiting":
        return
    wait.status = "resumed"
    wait.outcome = "approved" if decision.outcome in ("approved", "auto_approved") else "rejected"
    await db.flush()
    emit(
        db,
        WorkflowWaitResumed(
            tenant_id=wait.tenant_id,
            wait_id=wait.id,
            execution_id=wait.execution_id,
            rule_id=wait.rule_id,
            outcome=wait.outcome,
            origin_chain_depth=wait.chain_depth,
        ),
    )


async def describe(
    ctx: RequestContext, db: AsyncSession, subject_ids: list[uuid.UUID]
) -> dict[uuid.UUID, SubjectSummary]:
    if not subject_ids:
        return {}
    rows = await db.execute(
        select(WorkflowWait.id, WorkflowRule.name)
        .join(WorkflowRule, WorkflowRule.id == WorkflowWait.rule_id)
        .where(WorkflowWait.id.in_(subject_ids))
    )
    return {wid: SubjectSummary(label="Workflow approval", title=name) for wid, name in rows.tuples().all()}


def register_subject() -> None:
    from app.services import approvals

    approvals.register_approval_subject(SUBJECT, on_final_decided=on_decided, describe=describe)


# ================================================================ continue / resume
def _bind(db: AsyncSession, rule: WorkflowRule | None, chain_depth: int) -> Any:
    from app.services.workflow import engine

    if rule is None:
        return None
    token = set_context(engine.workflow_context(rule.tenant_id, rule.id, rule.created_by, chain_depth))
    db.sync_session.info[audit.ACTOR_INFO_KEY] = ("workflow", rule.id)
    return token


def _unbind(db: AsyncSession, token: Any) -> None:
    db.sync_session.info.pop(audit.ACTOR_INFO_KEY, None)
    if token is not None:
        reset_context(token)


async def _continue_locked(db: AsyncSession, wait: WorkflowWait) -> str:
    from app.services.workflow import executor

    wait.continued_at = _now()
    rule = await db.get(WorkflowRule, wait.rule_id)
    token = _bind(db, rule, wait.chain_depth)
    try:
        return await executor.resume(db, wait)
    finally:
        _unbind(db, token)


async def continue_run(db: AsyncSession, wait_id: uuid.UUID) -> str | None:
    """``workflow.continue_run``: continue a run whose approval was decided (idempotent)."""
    wait = (
        await db.execute(
            select(WorkflowWait)
            .where(
                WorkflowWait.id == wait_id,
                WorkflowWait.status.in_(("resumed", "timed_out")),
                WorkflowWait.continued_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if wait is None:
        return None
    outcome = await _continue_locked(db, wait)
    await db.commit()
    return outcome


async def _system_cancel_approval(db: AsyncSession, wait: WorkflowWait) -> None:
    from app.services import approvals

    ctx = RequestContext(tenant_id=wait.tenant_id, actor_kind="system")
    await approvals.cancel_open(ctx, db, SUBJECT, wait.id)


async def resume_waits(db: AsyncSession, now: datetime | None = None) -> int:
    """Beat body: resume due delays and time out approvals; each wait commits on its own."""
    now = now or _now()
    batch = get_settings().workflow_wait_batch
    due = list(
        (
            await db.execute(
                select(WorkflowWait.id)
                .where(
                    WorkflowWait.status == "waiting",
                    or_(
                        (WorkflowWait.kind == "delay") & (WorkflowWait.resume_at <= now),
                        (WorkflowWait.kind == "approval") & (WorkflowWait.timeout_at <= now),
                    ),
                )
                .order_by(WorkflowWait.created_at)
                .limit(batch)
            )
        ).scalars()
    )
    handled = 0
    for wait_id in due:
        try:
            wait = (
                await db.execute(
                    select(WorkflowWait)
                    .where(WorkflowWait.id == wait_id, WorkflowWait.status == "waiting")
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if wait is None:
                await db.rollback()
                continue
            if wait.kind == "delay":
                wait.status, wait.outcome = "resumed", "elapsed"
            else:
                await _system_cancel_approval(db, wait)
                wait.status, wait.outcome = "timed_out", "timeout"
            await db.flush()
            await _continue_locked(db, wait)
            await db.commit()
            handled += 1
        except Exception:
            await db.rollback()
            logger.exception("Workflow wait {} could not resume", wait_id)
    return handled


async def cancel_rule_waits(db: AsyncSession, rule_id: uuid.UUID, reason: str = "rule_deactivated") -> int:
    """Cancel every waiting row of a rule (and their open approval requests, as the system)."""
    waits = list(
        (
            await db.execute(
                select(WorkflowWait)
                .where(WorkflowWait.rule_id == rule_id, WorkflowWait.status == "waiting")
                .with_for_update()
            )
        ).scalars()
    )
    for wait in waits:
        if wait.kind == "approval":
            await _system_cancel_approval(db, wait)
        wait.status, wait.outcome = "cancelled", reason
        execution = await db.get(WorkflowExecution, wait.execution_id)
        if execution is not None and execution.status == "waiting":
            trace = list(execution.node_trace or [])
            trace.append(
                {
                    "node_id": wait.node_id,
                    "type": wait.kind,
                    "status": "cancelled",
                    "detail": reason,
                    "at": _now().isoformat(),
                }
            )
            execution.node_trace = trace
            execution.status, execution.finished_at = "cancelled", _now()
    await db.flush()
    return len(waits)


async def list_waits(db: AsyncSession, execution_id: uuid.UUID) -> list[WorkflowWait]:
    rows = await db.execute(
        select(WorkflowWait)
        .where(WorkflowWait.execution_id == execution_id)
        .order_by(WorkflowWait.created_at)
    )
    return list(rows.scalars())
