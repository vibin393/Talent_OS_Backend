# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Workflow rule administration (FR-58/FR-59): CRUD, activation (<= 100 active per tenant), executions,
dry runs and the registry the rule builder renders. New rules are always created inactive.

Epic 32 (AD-31): a rule body may carry a v2 ``graph`` instead of ``conditions`` / ``actions`` (422
``graph_and_form`` for both). A graph that ``to_p1`` can express is stored in P1 form (``graph=NULL``) and keeps
running on the P1 engine; others are stored as graphs (``definition_version=2``). Every create / change writes
an immutable ``workflow_rule_versions`` row (runs and waits pin it). ``RuleOut`` always carries the graph (stored
or derived) and its outline. Deactivating / archiving cancels waiting runs and their approvals."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.events import resolve_event_class
from app.models.workflow import WorkflowExecution, WorkflowRule, WorkflowRuleVersion, WorkflowWait
from app.schemas.workflow import (
    ActionTypeOut,
    ContractActionOut,
    DraftDryRunIn,
    DryRunIn,
    DryRunOut,
    ExecutionOut,
    FieldOut,
    OutlineRow,
    PolicyRef,
    RegistryOut,
    RuleIn,
    RuleOut,
    RuleUpdate,
    TriggerOut,
    WaitOut,
)
from app.services.workflow import catalogue, conditions, engine, executor, waits
from app.services.workflow import graph as graphs
from app.services.workflow.actions import (
    ACTIONS,
    HUMAN_ONLY_STAGES,
    MAX_ACTIONS,
    check_graph_actions,
    params_schemas,
    validate_actions,
)
from app.services.workflow.registry import TRIGGERS, fields_for

MAX_ACTIVE_RULES = 100
EXECUTION_RETENTION = timedelta(days=180)
_P1_TRIGGER_KEYS = frozenset(
    {
        "demand.created", "demand.status_changed", "demand.sla_at_risk", "demand.sla_breached",
        "application.stage_changed", "submission.created", "client_feedback.received", "interview.scheduled",
        "interview.completed", "offer.accepted", "offer.declined", "candidate.created",
    }
)  # fmt: skip


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def _validate(trigger: str, cond: Any, actions: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if trigger not in TRIGGERS:
        raise ValidationFailed(
            f"Unknown trigger '{trigger}'",
            code="invalid_trigger",
            details={"path": "trigger", "reason": "unknown"},
        )
    normalized = conditions.validate(cond, fields_for(trigger))
    return normalized, validate_actions(actions, TRIGGERS[trigger].entity_type)


Definition = tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]


async def _check_policies(db: AsyncSession, graph: dict[str, Any]) -> None:
    from app.services.sla import escalations

    for path, policy_id in graphs.escalation_policy_ids(graph):
        if await escalations.active_library_policy(db, policy_id) is None:
            raise ValidationFailed(
                f"Unknown or archived escalation policy at {path}",
                code="invalid_graph",
                details={"path": path, "reason": "unknown_escalation_policy"},
            )


def _check_allowed_actions(trigger: str, graph: dict[str, Any]) -> None:
    """Epic 48 (decision 9): a trigger with ``allowed_actions`` refuses every other action type (422)."""
    from app.services.workflow.registry import disallowed_actions

    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    types = [
        str((n.get("action") or {}).get("type"))
        for n in nodes
        if isinstance(n, dict) and n.get("type") == "action" and isinstance(n.get("action"), dict)
    ]
    refused = disallowed_actions(trigger, types)
    if refused:
        raise ValidationFailed(
            f"Action '{refused[0]}' is not allowed for trigger '{trigger}'",
            code="action_not_allowed_for_trigger",
            details={"path": "actions", "reason": "action_not_allowed_for_trigger", "actions": refused},
        )


async def _definition(db: AsyncSession, trigger: str, cond: Any, actions: Any, graph: Any) -> Definition:
    """(conditions, actions, graph) to store: the P1 form whenever the graph is expressible as one."""
    # Epic 48: Trigger.allowed_actions, checked on the raw input before parameter validation.
    _check_allowed_actions(
        trigger,
        graph
        if graph is not None
        else {"nodes": [{"type": "action", "action": a} for a in actions or [] if isinstance(a, dict)]},
    )
    if graph is None:
        cond, actions = _validate(trigger, cond, actions)
        await _check_policies(db, graphs.from_p1(cond, actions))
        await check_graph_actions(db, graphs.from_p1(cond, actions))  # Epic 38: async action checks
        return cond, actions, None
    normalized = graphs.validate_graph(graph, trigger)
    await _check_policies(db, normalized)
    await check_graph_actions(db, normalized)  # Epic 38: async action checks
    p1 = graphs.to_p1(normalized)
    if p1 is not None:
        return p1[0], p1[1], None
    return {}, [], normalized


def _graph_and_form(sent: set[str], data: Any) -> None:
    if data.graph is not None and (
        ("conditions" in sent and data.conditions) or ("actions" in sent and data.actions)
    ):
        raise ValidationFailed(
            "Send either a graph or conditions / actions, not both",
            code="graph_and_form",
            details={"path": "graph", "reason": "graph_and_form"},
        )


def _write_version(db: AsyncSession, rule: WorkflowRule) -> None:
    db.add(
        WorkflowRuleVersion(
            id=uuid.uuid4(),
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            version=rule.version,
            trigger=rule.trigger,
            conditions=dict(rule.conditions or {}),
            actions=list(rule.actions or []),
            graph=dict(rule.graph) if rule.graph else None,
        )
    )


async def _failures(db: AsyncSession, rule_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not rule_ids:
        return {}
    since = _now() - timedelta(hours=24)
    rows = await db.execute(
        select(WorkflowExecution.rule_id, func.count())
        .where(
            WorkflowExecution.rule_id.in_(rule_ids),
            WorkflowExecution.mode == "live",
            WorkflowExecution.created_at >= since,
            WorkflowExecution.status.in_(("failed", "partially_failed")),
        )
        .group_by(WorkflowExecution.rule_id)
    )
    return {rid: int(n) for rid, n in rows.tuples().all()}


async def _waiting(db: AsyncSession, rule_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not rule_ids:
        return {}
    rows = await db.execute(
        select(WorkflowWait.rule_id, func.count())
        .where(WorkflowWait.rule_id.in_(rule_ids), WorkflowWait.status == "waiting")
        .group_by(WorkflowWait.rule_id)
    )
    return {rid: int(n) for rid, n in rows.tuples().all()}


def _out(rule: WorkflowRule, failures: int = 0, waiting: int = 0) -> RuleOut:
    graph = dict(rule.graph) if rule.graph else graphs.from_p1(rule.conditions or {}, rule.actions or [])
    return RuleOut(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        trigger=rule.trigger,
        conditions=dict(rule.conditions or {}),
        actions=list(rule.actions or []),
        is_active=rule.is_active,
        version=rule.version,
        created_by=rule.created_by,
        updated_by=rule.updated_by,
        last_fired_at=rule.last_fired_at,
        archived_at=rule.archived_at,
        failures_24h=failures,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        graph=graph,
        definition_version=rule.definition_version or 1,
        outline=[OutlineRow(**row) for row in graphs.outline(graph, rule.trigger)],
        step_count=graphs.step_count(graph),
        is_p1_form=rule.graph is None,
        waiting_runs=waiting,
    )


async def _rule_out(db: AsyncSession, rule: WorkflowRule) -> RuleOut:
    await db.refresh(rule)
    return _out(
        rule,
        (await _failures(db, [rule.id])).get(rule.id, 0),
        (await _waiting(db, [rule.id])).get(rule.id, 0),
    )


async def _get(db: AsyncSession, rule_id: uuid.UUID, *, for_update: bool = False) -> WorkflowRule:
    stmt = select(WorkflowRule).where(WorkflowRule.id == rule_id)
    if for_update:
        stmt = stmt.with_for_update()
    rule = (await db.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise NotFound("Workflow rule not found")
    return rule


# ================================================================ CRUD
async def list_rules(
    ctx: RequestContext, db: AsyncSession, params: PageParams, include_archived: bool = False
) -> Page[RuleOut]:
    _principal(ctx)
    stmt = select(WorkflowRule)
    if not include_archived:
        stmt = stmt.where(WorkflowRule.archived_at.is_(None))
    page = await paginate(db, stmt.order_by(WorkflowRule.name, WorkflowRule.id), params)
    failures = await _failures(db, [r.id for r in page.items])
    waiting = await _waiting(db, [r.id for r in page.items])
    return Page[RuleOut](
        items=[_out(r, failures.get(r.id, 0), waiting.get(r.id, 0)) for r in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def create_rule(ctx: RequestContext, db: AsyncSession, data: RuleIn) -> RuleOut:
    principal = _principal(ctx)
    _graph_and_form(data.model_fields_set, data)
    cond, actions, graph = await _definition(db, data.trigger, data.conditions, data.actions, data.graph)
    rule = WorkflowRule(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        name=data.name,
        description=data.description,
        trigger=data.trigger,
        conditions=cond,
        actions=actions,
        graph=graph,
        definition_version=2 if graph is not None else 1,
        is_active=False,
        version=1,
        created_by=principal.id,
    )
    db.add(rule)
    await db.flush()
    _write_version(db, rule)
    await db.flush()
    return await _rule_out(db, rule)


async def get_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID) -> RuleOut:
    _principal(ctx)
    return await _rule_out(db, await _get(db, rule_id))


async def update_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID, data: RuleUpdate) -> RuleOut:
    principal = _principal(ctx)
    rule = await _get(db, rule_id, for_update=True)
    if rule.archived_at is not None:
        raise ConflictError("Archived rules cannot be edited", code="rule_archived")
    sent = data.model_fields_set
    _graph_and_form(sent, data)
    trigger = data.trigger if "trigger" in sent and data.trigger else rule.trigger
    form_sent = ("conditions" in sent and data.conditions is not None) or (
        "actions" in sent and data.actions is not None
    )
    graph_in: Any = None
    if "graph" in sent and data.graph is not None:
        graph_in = data.graph
    elif rule.graph is not None and not form_sent:
        graph_in = rule.graph  # a graph rule keeps its graph (re-validated for a new trigger)
    cond = data.conditions if "conditions" in sent and data.conditions is not None else rule.conditions
    actions = data.actions if "actions" in sent and data.actions is not None else rule.actions
    if graph_in is not None:
        cond, actions = {}, []
    cond, actions, graph = await _definition(db, trigger, cond, actions, graph_in)
    changed = False
    if graph != rule.graph:
        rule.graph, rule.definition_version, changed = graph, 2 if graph is not None else 1, True
    for attr, value in (("trigger", trigger), ("conditions", cond), ("actions", actions)):
        if getattr(rule, attr) != value:
            setattr(rule, attr, value)
            changed = True
    if "name" in sent and data.name is not None and data.name != rule.name:
        rule.name, changed = data.name, True
    if "description" in sent and data.description != rule.description:
        rule.description, changed = data.description, True
    if changed:
        rule.version = rule.version + 1
        rule.updated_by = principal.id
        await db.flush()
        _write_version(db, rule)  # waiting runs keep their pinned version
        await db.flush()
    return await _rule_out(db, rule)


async def set_active(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID, active: bool) -> RuleOut:
    principal = _principal(ctx)
    rule = await _get(db, rule_id, for_update=True)
    if active and rule.archived_at is not None:
        raise ConflictError("Archived rules cannot be activated", code="rule_archived")
    if active and not rule.is_active:
        count = (
            await db.execute(
                select(func.count())
                .select_from(WorkflowRule)
                .where(WorkflowRule.is_active.is_(True), WorkflowRule.archived_at.is_(None))
            )
        ).scalar_one()
        if int(count) >= MAX_ACTIVE_RULES:
            raise ConflictError(
                f"At most {MAX_ACTIVE_RULES} rules can be active",
                code="rule_limit",
                details={"limit": MAX_ACTIVE_RULES},
            )
    if rule.is_active != active:
        rule.is_active = active
        rule.updated_by = principal.id
        await db.flush()
        if not active:
            await waits.cancel_rule_waits(db, rule.id, "rule_deactivated")
    return await _rule_out(db, rule)


async def archive_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID) -> RuleOut:
    principal = _principal(ctx)
    rule = await _get(db, rule_id, for_update=True)
    if rule.archived_at is None:
        rule.archived_at, rule.is_active, rule.updated_by = _now(), False, principal.id
        await db.flush()
        await waits.cancel_rule_waits(db, rule.id, "rule_archived")
    return await _rule_out(db, rule)


# ================================================================ executions
def _wait_out(w: WorkflowWait) -> WaitOut:
    return WaitOut(
        id=w.id,
        node_id=w.node_id,
        kind=w.kind,
        status=w.status,
        outcome=w.outcome,
        resume_at=w.resume_at,
        timeout_at=w.timeout_at,
        approval_request_id=w.approval_request_id,
        created_at=w.created_at,
    )


def _execution_out(e: WorkflowExecution, wait_rows: list[WorkflowWait] | None = None) -> ExecutionOut:
    duration = (
        int((e.finished_at - e.started_at).total_seconds() * 1000) if e.finished_at and e.started_at else None
    )
    return ExecutionOut(
        id=e.id,
        rule_id=e.rule_id,
        rule_version=e.rule_version,
        mode=e.mode,
        trigger=e.trigger,
        event_id=e.event_id,
        entity_type=e.entity_type,
        entity_id=e.entity_id,
        chain_depth=e.chain_depth,
        status=e.status,
        condition_trace=list(e.condition_trace or []),
        action_results=list(e.action_results or []),
        enqueued_at=e.enqueued_at,
        started_at=e.started_at,
        finished_at=e.finished_at,
        duration_ms=duration,
        created_at=e.created_at,
        node_trace=list(e.node_trace or []),
        waits=[_wait_out(w) for w in wait_rows or []],
    )


async def list_executions(
    ctx: RequestContext,
    db: AsyncSession,
    rule_id: uuid.UUID,
    params: PageParams,
    status: str | None = None,
    mode: str | None = None,
) -> Page[ExecutionOut]:
    _principal(ctx)
    await _get(db, rule_id)
    stmt = select(WorkflowExecution).where(WorkflowExecution.rule_id == rule_id)
    if status:
        stmt = stmt.where(WorkflowExecution.status == status)
    if mode:
        stmt = stmt.where(WorkflowExecution.mode == mode)
    page = await paginate(
        db, stmt.order_by(WorkflowExecution.created_at.desc(), WorkflowExecution.id), params
    )
    return Page[ExecutionOut](
        items=[_execution_out(e) for e in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def get_execution(ctx: RequestContext, db: AsyncSession, execution_id: uuid.UUID) -> ExecutionOut:
    _principal(ctx)
    execution = await db.get(WorkflowExecution, execution_id)
    if execution is None:
        raise NotFound("Workflow execution not found")
    return _execution_out(execution, await waits.list_waits(db, execution.id))


async def purge_executions(db: AsyncSession, now: datetime | None = None) -> int:
    from sqlalchemy import delete

    cutoff = (now or _now()) - EXECUTION_RETENTION
    # Runs still parked on a wait are kept (their wait resumes them).
    result = await db.execute(
        delete(WorkflowExecution).where(
            WorkflowExecution.created_at < cutoff, WorkflowExecution.status != "waiting"
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ================================================================ dry runs
async def dry_run_rule(
    ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID, data: DryRunIn
) -> DryRunOut:
    _principal(ctx)
    rule = await _get(db, rule_id)
    if rule.graph is not None:
        result = await executor.dry_run(
            ctx,
            db,
            rule_id=rule.id,
            rule_version=rule.version,
            created_by=rule.created_by,
            trigger=rule.trigger,
            graph=rule.graph,
            entity_id=data.entity_id,
            extras=data.extras,
            assume_approval=data.assume_approval,
        )
        return DryRunOut(**result)
    result = await engine.dry_run(
        ctx,
        db,
        rule_id=rule.id,
        rule_version=rule.version,
        created_by=rule.created_by,
        trigger=rule.trigger,
        rule_conditions=rule.conditions or {},
        actions=rule.actions or [],
        entity_id=data.entity_id,
        extras=data.extras,
    )
    return DryRunOut(**result)


async def dry_run_draft(ctx: RequestContext, db: AsyncSession, data: DraftDryRunIn) -> DryRunOut:
    principal = _principal(ctx)
    _graph_and_form(data.rule.model_fields_set, data.rule)
    cond, actions, graph = await _definition(
        db, data.rule.trigger, data.rule.conditions, data.rule.actions, data.rule.graph
    )
    if graph is not None:
        result = await executor.dry_run(
            ctx,
            db,
            rule_id=None,
            rule_version=0,
            created_by=principal.id,
            trigger=data.rule.trigger,
            graph=graph,
            entity_id=data.entity_id,
            extras=data.extras,
            assume_approval=data.assume_approval,
        )
        return DryRunOut(**result)
    result = await engine.dry_run(
        ctx,
        db,
        rule_id=None,
        rule_version=0,
        created_by=principal.id,
        trigger=data.rule.trigger,
        rule_conditions=cond,
        actions=actions,
        entity_id=data.entity_id,
        extras=data.extras,
    )
    return DryRunOut(**result)


# ================================================================ registry
async def registry_for(ctx: RequestContext, db: AsyncSession) -> RegistryOut:  # ruff: ignore[unused-function-argument]
    """The builder registry plus the tenant's library escalation policies (Epic 32)."""
    from app.services.sla import escalations

    out = registry()
    out.escalation_policies = [
        PolicyRef(id=pid, name=name) for pid, name in await escalations.policy_names(db)
    ]
    return out


def _group(key: str) -> str:
    if key in _P1_TRIGGER_KEYS:
        return "Phase 1"
    return key.split(".", 1)[0].replace("_", " ").title()


def _allowed_list(key: str) -> list[str] | None:
    from app.services.workflow.registry import allowed_actions_for

    allowed = allowed_actions_for(key)
    return sorted(allowed) if allowed is not None else None


def registry() -> RegistryOut:
    settings = get_settings()
    triggers = []
    for key, trigger in TRIGGERS.items():
        fields = [
            FieldOut(
                path=path,
                label=f.label,
                type=f.type,
                values=list(f.values),
                ops=sorted(conditions.OPS_BY_TYPE[f.type]),
            )
            for path, f in fields_for(key).items()
        ]
        triggers.append(
            TriggerOut(
                key=key,
                label=trigger.label,
                entity_type=trigger.entity_type,
                available=resolve_event_class(trigger.event_module, trigger.event_class) is not None,
                fields=fields,
                group=_group(key),
                owner_epic=catalogue.owner_of(key),
                allowed_actions=_allowed_list(key),
            )
        )
    return RegistryOut(
        triggers=triggers,
        ops=list(conditions.OPS),
        actions=[
            ActionTypeOut(type=k, entity_types=list(getattr(ACTIONS[k], "entity_types", ()) or ()), **v)
            for k, v in params_schemas().items()
        ],
        move_stage_blocked_targets=sorted(HUMAN_ONLY_STAGES),
        limits={
            "max_actions": MAX_ACTIONS,
            "max_clauses": conditions.MAX_CLAUSES,
            "max_depth": conditions.MAX_DEPTH,
            "max_active_rules": MAX_ACTIVE_RULES,
            "max_steps": settings.workflow_graph_max_steps,
            "max_delay_days": settings.workflow_max_delay_days,
        },
        node_types=list(graphs.NODE_TYPES),
        delay_units=list(graphs.DELAY_UNITS),
        approver_kinds=list(graphs.APPROVER_KINDS),
        max_steps=settings.workflow_graph_max_steps,
        contract_actions=[
            ContractActionOut(type=k, owner_epic=owner, available=k in ACTIONS)
            for k, owner in sorted(catalogue.ACTION_CATALOGUE.items())
        ],
    )
