# ruff: file-ignore[line-too-long]
"""Graph-rule executor (Epic 32, story 19.3, AD-31). Rules with ``graph IS NULL`` never come here: the P1
engine runs them byte-for-byte.

A live run starts from ``engine.evaluate_event`` with the P1 claim window, loop protection (``chain_depth``) and
idempotency, then walks the graph of the pinned ``rule_version``:

* ``condition`` -- evaluate on the snapshot (+ ``event.*`` extras) and take ``then`` / ``else``;
* ``action`` -- run the registered action in its own savepoint as actor ``workflow``; a failure is recorded and
  the walk continues (P1 partial-failure semantics);
* ``delay`` -- insert a ``workflow_waits`` row (``resume_at`` from ``core.business_hours`` or wall clock), stop;
* ``approval`` -- start an approval (subject ``workflow_step`` = the wait) with one explicit step, insert the
  wait, stop. The approval only picks the branch; it never transitions the trigger entity;
* ``end`` -- finish.

Resumes (``resume``) reload a fresh snapshot, re-check that the rule is still active and the entity exists
(else the run is ``cancelled``), and continue on the pinned version. The execution row is updated in place
(``status=waiting`` while parked); ``node_trace`` records every node.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_hours import tenant_calendar
from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound
from app.core.events import emit
from app.core.settings import get_settings
from app.events.workflow import WorkflowExecutionFailed, WorkflowRunCompleted, WorkflowWaitCreated
from app.models.workflow import WorkflowExecution, WorkflowRule, WorkflowRuleVersion, WorkflowWait
from app.services import entity_refs
from app.services.sla import durations
from app.services.workflow import conditions
from app.services.workflow.graph import by_id
from app.services.workflow.registry import TRIGGERS, fields_for

MAX_WALK = 200  # safety net; graphs are acyclic and <= 25 steps by validation


def _now() -> datetime:
    return datetime.now(UTC)


class Run:
    """The state of one walk (live or dry)."""

    def __init__(
        self,
        *,
        db: AsyncSession,
        ctx: RequestContext,
        rule_id: uuid.UUID | None,
        rule_version: int,
        created_by: uuid.UUID,
        trigger: str,
        graph: Mapping[str, Any],
        entity_type: str,
        entity_id: uuid.UUID,
        flat: dict[str, Any],
        extras: Mapping[str, Any],
        chain_depth: int,
        execution: WorkflowExecution | None,
    ) -> None:
        self.db = db
        self.ctx = ctx
        self.rule_id = rule_id
        self.rule_version = rule_version
        self.created_by = created_by
        self.trigger = trigger
        self.graph = graph
        self.nodes = by_id(graph)
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.flat = flat
        self.extras = dict(extras)
        self.chain_depth = chain_depth
        self.execution = execution
        self.trace: list[dict[str, Any]] = list(execution.node_trace or []) if execution is not None else []
        self.condition_trace: list[dict[str, Any]] = (
            list(execution.condition_trace or []) if execution else []
        )
        self.action_results: list[dict[str, Any]] = list(execution.action_results or []) if execution else []
        self.path: list[dict[str, Any]] = []

    def ac_base(self) -> dict[str, Any]:
        return {
            "ctx": self.ctx,
            "db": self.db,
            "tenant_id": entity_refs.session_tenant(self.db),
            "rule_id": self.rule_id,
            "rule_created_by": self.created_by,
            "trigger": self.trigger,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "snapshot": self.flat,
            "execution_id": self.execution.id if self.execution is not None else None,
        }

    def note(self, node: Mapping[str, Any], status: str, detail: Any = None) -> None:
        self.trace.append(
            {
                "node_id": node["id"],
                "type": node["type"],
                "status": status,
                "detail": detail,
                "at": _now().isoformat(),
            }
        )

    def save(self) -> None:
        if self.execution is not None:
            self.execution.node_trace = list(self.trace)
            self.execution.condition_trace = list(self.condition_trace)
            self.execution.action_results = list(self.action_results)


def _outcome(results: list[dict[str, Any]]) -> str:
    errors = sum(1 for r in results if r.get("status") == "error")
    if errors == 0:
        return "succeeded"
    return "failed" if errors == len(results) else "partially_failed"


# ================================================================ approval nodes
async def _approver(run: Run, approver: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    from app.services.workflow.actions import ActionContext, _select_user

    kind, ref = str(approver.get("kind")), approver.get("ref")
    if kind == "user":
        return "user", {"user_id": str(ref)}
    if kind == "role":
        return "role", {"role_id": str(ref)}
    ac = ActionContext(params=None, **run.ac_base())
    user_id = await _select_user(ac, "entity_owner" if kind == "entity_owner" else "primary_recruiter")
    return ("user", {"user_id": str(user_id)}) if user_id else None


async def _escalation(db: AsyncSession, policy_id: Any) -> Any:
    if not policy_id:
        return None
    from app.services.approvals import EscalationSpec
    from app.services.sla import escalations

    policy = await escalations.active_library_policy(db, uuid.UUID(str(policy_id)))
    if policy is None:
        return None
    return EscalationSpec(
        after_business_hours=Decimal(policy.after_business_hours),
        action=policy.action,  # type: ignore[arg-type]
        target_kind=policy.target_kind,
        target_ref=policy.target_ref,
        repeat_every_hours=Decimal(policy.repeat_every_hours)
        if policy.repeat_every_hours is not None
        else None,
        max_repeats=int(policy.max_repeats),
    )


async def _park(run: Run, node: Mapping[str, Any], kind: str, *, resume_at: datetime | None) -> WorkflowWait:
    assert run.execution is not None and run.rule_id is not None
    wait = WorkflowWait(
        id=uuid.uuid4(),
        tenant_id=entity_refs.session_tenant(run.db),
        execution_id=run.execution.id,
        rule_id=run.rule_id,
        rule_version=run.rule_version,
        node_id=str(node["id"]),
        kind=kind,
        resume_at=resume_at,
        status="waiting",
        entity_type=run.entity_type,
        entity_id=run.entity_id,
        chain_depth=run.chain_depth,
        extras=run.extras,
    )
    run.db.add(wait)
    await run.db.flush()
    return wait


async def _start_approval(run: Run, node: Mapping[str, Any], now: datetime) -> bool:
    """Create the wait + approval request; False (recorded as an error) when it cannot start."""
    from app.services import approvals

    resolved = await _approver(run, node["approver"])
    if resolved is None:
        run.note(node, "error", "approver_unresolved")
        return False
    cal, tz = await tenant_calendar(run.db)
    timeout = node.get("timeout")
    timeout_at = durations.add(now, timeout["value"], timeout["unit"], cal, tz) if timeout else None
    try:
        async with run.db.begin_nested():
            wait = await _park(run, node, "approval", resume_at=None)
            wait.timeout_at = timeout_at
            result = await approvals.start(
                run.ctx,
                run.db,
                "workflow_step",
                wait.id,
                {
                    "rule_id": str(run.rule_id),
                    "node_id": str(node["id"]),
                    "title": str(node.get("title")),
                    "entity_type": run.entity_type,
                    "entity_id": str(run.entity_id),
                },
                steps=[
                    approvals.StepSpec(
                        name=str(node.get("title"))[:200],
                        approver_kind=resolved[0],
                        approver_ref=resolved[1],
                        escalation=await _escalation(run.db, node.get("escalation_policy_id")),
                    )
                ],
            )
            wait.approval_request_id = result.request_id
            await run.db.flush()
    except DomainError as exc:
        run.note(node, "error", exc.code)
        return False
    run.note(
        node,
        "waiting",
        {
            "approval_request_id": str(wait.approval_request_id),
            "timeout_at": timeout_at.isoformat() if timeout_at else None,
        },
    )
    emit(
        run.db,
        WorkflowWaitCreated(
            tenant_id=wait.tenant_id,
            wait_id=wait.id,
            execution_id=wait.execution_id,
            rule_id=wait.rule_id,
            kind="approval",
            origin_chain_depth=run.chain_depth,
        ),
    )
    return True


# ================================================================ walk
async def walk(run: Run, node_id: str | None, *, dry: bool = False, assume: str = "approved") -> str:
    """Walk from ``node_id``; returns ``waiting`` (parked) or ``done``."""
    from app.services.workflow.engine import run_actions

    fields = fields_for(run.trigger) if run.trigger in TRIGGERS else {}
    cal, tz = await tenant_calendar(run.db)
    zone = ZoneInfo(tz)
    cursor = _now()
    for _ in range(MAX_WALK):
        node = run.nodes.get(str(node_id)) if node_id else None
        if node is None or node["type"] == "end":
            if node is not None:
                run.path.append(
                    {"node_id": node["id"], "type": "end", "outcome": "end", "scheduled_at": None}
                )
            return "done"
        kind = node["type"]
        if kind == "condition":
            matched, trace = conditions.evaluate(node.get("when") or {}, run.flat, fields)
            run.condition_trace.extend({**t, "node_id": node["id"]} for t in trace)
            run.note(node, "yes" if matched else "no")
            run.path.append(
                {
                    "node_id": node["id"],
                    "type": kind,
                    "outcome": "yes" if matched else "no",
                    "scheduled_at": None,
                }
            )
            node_id = node.get("then") if matched else node.get("else")
        elif kind == "action":
            [entry] = await run_actions(run.ac_base(), [node["action"]], dry=dry)
            entry = {**entry, "index": len(run.action_results), "node_id": node["id"]}
            run.action_results.append(entry)
            run.note(node, entry["status"], entry.get("detail"))
            run.path.append(
                {"node_id": node["id"], "type": kind, "outcome": entry["status"], "scheduled_at": None}
            )
            node_id = node.get("next")
        elif kind == "delay":
            d = node["duration"]
            resume_at = durations.add(cursor, d["value"], d["unit"], cal, tz)
            if dry:
                run.path.append(
                    {
                        "node_id": node["id"],
                        "type": kind,
                        "outcome": "wait",
                        "scheduled_at": resume_at.astimezone(zone),
                    }
                )
                cursor = resume_at
                node_id = node.get("next")
                continue
            wait = await _park(run, node, "delay", resume_at=resume_at)
            run.note(node, "waiting", {"resume_at": resume_at.isoformat()})
            emit(
                run.db,
                WorkflowWaitCreated(
                    tenant_id=wait.tenant_id,
                    wait_id=wait.id,
                    execution_id=wait.execution_id,
                    rule_id=wait.rule_id,
                    kind="delay",
                    origin_chain_depth=run.chain_depth,
                ),
            )
            return "waiting"
        elif kind == "approval":
            if dry:
                scheduled = None
                if assume == "timeout" and node.get("timeout"):
                    t = node["timeout"]
                    scheduled = durations.add(cursor, t["value"], t["unit"], cal, tz)
                    cursor = scheduled
                outcome = assume if (assume != "timeout" or node.get("timeout")) else "approved"
                run.path.append(
                    {
                        "node_id": node["id"],
                        "type": kind,
                        "outcome": outcome,
                        "scheduled_at": scheduled.astimezone(zone) if scheduled else None,
                    }
                )
                node_id = node.get(
                    {"approved": "on_approved", "rejected": "on_rejected", "timeout": "on_timeout"}[outcome]
                )
                continue
            if await _start_approval(run, node, cursor):
                return "waiting"
            run.action_results.append(
                {
                    "index": len(run.action_results),
                    "node_id": node["id"],
                    "type": "approval",
                    "status": "error",
                    "detail": "The approval could not start",
                    "error_code": "approver_unresolved",
                }
            )
            return "done"
        else:  # pragma: no cover - validated
            return "done"
    logger.warning("Workflow walk exceeded {} nodes (rule {})", MAX_WALK, run.rule_id)
    return "done"


def _finish(run: Run, outcome: str) -> None:
    execution = run.execution
    assert execution is not None
    run.save()
    if outcome == "waiting":
        execution.status = "waiting"
        return
    execution.status = _outcome(run.action_results) if run.action_results else "succeeded"
    execution.finished_at = _now()
    emit(
        run.db,
        WorkflowRunCompleted(
            tenant_id=execution.tenant_id,
            execution_id=execution.id,
            rule_id=execution.rule_id,
            status=execution.status,
            origin_chain_depth=run.chain_depth,
        ),
    )
    if execution.status in ("failed", "partially_failed"):
        emit(
            run.db,
            WorkflowExecutionFailed(
                tenant_id=execution.tenant_id,
                execution_id=execution.id,
                rule_id=execution.rule_id,
                status=execution.status,
            ),
        )


# ================================================================ live start
async def start_run(
    db: AsyncSession,
    rule: WorkflowRule,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    event_id: uuid.UUID,
    extras: Mapping[str, Any],
    chain_depth: int,
    enqueued_at: datetime | None,
) -> uuid.UUID | None:
    from app.services.workflow import engine

    if await engine._existing(db, rule.id, event_id, entity_id):
        return None
    now = _now()
    execution = WorkflowExecution(
        id=uuid.uuid4(),
        tenant_id=rule.tenant_id,
        rule_id=rule.id,
        rule_version=rule.version,
        mode="live",
        trigger=rule.trigger,
        event_id=event_id,
        entity_type=entity_type,
        entity_id=entity_id,
        chain_depth=chain_depth,
        status="matched",
        enqueued_at=enqueued_at,
        started_at=now,
    )
    if chain_depth > get_settings().workflow_max_chain_depth:
        execution.status = "skipped_loop"
    elif not await engine._claim_window(db, rule.tenant_id, rule.id, entity_id):
        execution.status = "skipped_rate_limit"
    try:
        async with db.begin_nested():
            db.add(execution)
            await db.flush()
    except IntegrityError:
        return None
    if execution.status != "matched":
        execution.finished_at = _now()
        await db.flush()
        return execution.id
    ctx = engine.workflow_context(rule.tenant_id, rule.id, rule.created_by, chain_depth)
    snapshot = await engine.load_snapshot(ctx, db, entity_type, entity_id)
    flat = engine._with_extras(snapshot, extras)
    rule.last_fired_at = now
    run = Run(
        db=db,
        ctx=ctx,
        rule_id=rule.id,
        rule_version=rule.version,
        created_by=rule.created_by,
        trigger=rule.trigger,
        graph=rule.graph or {},
        entity_type=entity_type,
        entity_id=entity_id,
        flat=flat,
        extras=extras,
        chain_depth=chain_depth,
        execution=execution,
    )
    if snapshot is None:
        run.condition_trace.insert(
            0, {"path": "snapshot", "result": False, "note": "entity snapshot unavailable"}
        )
    outcome = await walk(run, str(run.graph.get("start")))
    # A run whose first condition fails without reaching any action is ``not_matched`` (P1 semantics).
    first = run.path[0] if run.path else None
    if (
        outcome == "done"
        and not run.action_results
        and first
        and first["type"] == "condition"
        and first["outcome"] == "no"
        and len(run.path) <= 2
    ):
        run.save()
        execution.status, execution.finished_at = "not_matched", _now()
    else:
        _finish(run, outcome)
    await db.flush()
    return execution.id


# ================================================================ resume
async def pinned_graph(db: AsyncSession, rule_id: uuid.UUID, version: int) -> dict[str, Any] | None:
    row = (
        await db.execute(
            select(WorkflowRuleVersion).where(
                WorkflowRuleVersion.rule_id == rule_id, WorkflowRuleVersion.version == version
            )
        )
    ).scalar_one_or_none()
    return dict(row.graph) if row is not None and row.graph else None


async def _cancel(
    db: AsyncSession, execution: WorkflowExecution | None, wait: WorkflowWait, reason: str
) -> None:
    wait.status, wait.outcome = "cancelled", reason
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


async def resume(db: AsyncSession, wait: WorkflowWait) -> str:
    """Continue the run parked on ``wait`` (already ``resumed`` / ``timed_out``) from the node after it."""
    from app.services.workflow import engine

    execution = await db.get(WorkflowExecution, wait.execution_id)
    rule = await db.get(WorkflowRule, wait.rule_id)
    if execution is None:
        return "gone"
    if rule is None or not rule.is_active or rule.archived_at is not None:
        await _cancel(db, execution, wait, "rule_inactive")
        return "cancelled"
    graph = await pinned_graph(db, rule.id, wait.rule_version)
    if graph is None:
        await _cancel(db, execution, wait, "version_missing")
        return "cancelled"
    ctx = engine.workflow_context(rule.tenant_id, rule.id, rule.created_by, wait.chain_depth)
    snapshot = await engine.load_snapshot(ctx, db, wait.entity_type, wait.entity_id)
    if snapshot is None:
        await _cancel(db, execution, wait, "entity_gone")
        return "cancelled"
    run = Run(
        db=db,
        ctx=ctx,
        rule_id=rule.id,
        rule_version=wait.rule_version,
        created_by=rule.created_by,
        trigger=execution.trigger,
        graph=graph,
        entity_type=wait.entity_type,
        entity_id=wait.entity_id,
        flat=engine._with_extras(snapshot, wait.extras),
        extras=wait.extras,
        chain_depth=wait.chain_depth,
        execution=execution,
    )
    node = run.nodes.get(wait.node_id)
    if node is None:
        await _cancel(db, execution, wait, "node_missing")
        return "cancelled"
    if wait.kind == "delay":
        nxt = node.get("next")
        run.note(node, "resumed")
    else:
        branch = {"approved": "on_approved", "rejected": "on_rejected", "timeout": "on_timeout"}.get(
            wait.outcome or "", "on_rejected"
        )
        nxt = node.get(branch)
        run.note(node, wait.outcome or "rejected")
    outcome = await walk(run, nxt)
    _finish(run, outcome)
    await db.flush()
    return outcome


# ================================================================ dry run
async def dry_run(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    rule_id: uuid.UUID | None,
    rule_version: int,
    created_by: uuid.UUID,
    trigger: str,
    graph: Mapping[str, Any],
    entity_id: uuid.UUID,
    extras: Mapping[str, Any] | None,
    assume_approval: str = "approved",
) -> dict[str, Any]:
    """Walk without side effects (no wait, approval, task, email or notification); the path carries the
    computed times of delays and approval timeouts in the tenant timezone."""
    from app.services.workflow import engine

    entity_type = TRIGGERS[trigger].entity_type
    tenant_id = entity_refs.session_tenant(db)
    if entity_refs.is_access_checked(entity_type):
        await entity_refs.require_readable(ctx, db, entity_type, entity_id)
    wctx = engine.workflow_context(tenant_id, rule_id, created_by, 0)
    snapshot = await engine.load_snapshot(wctx, db, entity_type, entity_id)
    if snapshot is None:
        raise NotFound("Resource not found")
    run = Run(
        db=db,
        ctx=wctx,
        rule_id=rule_id,
        rule_version=rule_version,
        created_by=created_by,
        trigger=trigger,
        graph=graph,
        entity_type=entity_type,
        entity_id=entity_id,
        flat=engine._with_extras(snapshot, extras),
        extras=extras or {},
        chain_depth=0,
        execution=None,
    )
    await walk(run, str(graph.get("start")), dry=True, assume=assume_approval)
    first = run.path[0] if run.path else None
    matched = not (
        first and first["type"] == "condition" and first["outcome"] == "no" and not run.action_results
    )
    execution_id: uuid.UUID | None = None
    if rule_id is not None:
        now = _now()
        execution = WorkflowExecution(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            rule_id=rule_id,
            rule_version=rule_version,
            mode="dry_run",
            trigger=trigger,
            event_id=uuid.uuid4(),
            entity_type=entity_type,
            entity_id=entity_id,
            chain_depth=0,
            status="matched" if matched else "not_matched",
            condition_trace=run.condition_trace,
            action_results=run.action_results,
            node_trace=run.trace,
            enqueued_at=now,
            started_at=now,
            finished_at=now,
        )
        db.add(execution)
        await db.flush()
        execution_id = execution.id
    return {
        "matched": matched,
        "condition_trace": run.condition_trace,
        "action_results": run.action_results,
        "execution_id": execution_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "path": run.path,
    }
