"""Workflow engine (FR-58, FR-59): live rule runs (Celery ``workflow.evaluate_event``) and dry runs.

Live run, per active rule of the trigger:
1. idempotency -- an execution for ``(rule, event, entity, live)`` already exists: skip (task retry);
2. loop protection -- ``chain_depth > WORKFLOW_MAX_CHAIN_DEPTH`` -> ``skipped_loop``; Redis
   ``SET wf:{tenant}:{rule}:{entity} NX EX 60`` fails -> ``skipped_rate_limit`` (Redis down: a live execution
   of the same rule and entity within 60 s);
3. conditions on the entity snapshot (+ ``event.*`` extras) -> ``not_matched`` with the trace;
4. actions in order, each in its own savepoint; failures are recorded and the rest continue ->
   ``succeeded | partially_failed | failed`` (a failure emits ``WorkflowExecutionFailed``);
5. the execution row commits together with the action effects.

Actions run with ``RequestContext(actor_kind="workflow", actor_id=<rule id>)`` and audit actor ``workflow``.
Dry runs evaluate and call each action's ``dry_run`` -- no writes, events, emails or tasks, except the
``mode=dry_run`` execution row of a saved rule.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import Boolean, Uuid, column, select, table
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext, reset_context, set_context
from app.core.errors import DomainError, NotFound
from app.core.events import emit
from app.core.permissions import Principal, Scope
from app.core.settings import get_settings
from app.events.workflow import WorkflowExecutionFailed
from app.models.workflow import WorkflowExecution, WorkflowRule
from app.services import entity_refs
from app.services.workflow import conditions
from app.services.workflow.actions import ActionContext, ActionError, parse_params
from app.services.workflow.registry import TRIGGERS, fields_for

RATE_WINDOW_SECONDS = 60
# What a rule may touch through the services it calls (never approve / submit / offer / demand status).
WORKFLOW_PERMISSIONS: tuple[str, ...] = (
    "demand:read",
    "demand:assign",
    "candidate:read",
    "candidate:update",
    "client:read",
    "contact:read",
    "application:read",
    "application:move",
    "task:read",
    "task:create",
)


def _now() -> datetime:
    return datetime.now(UTC)


def workflow_context(
    tenant_id: uuid.UUID, rule_id: uuid.UUID | None, created_by: uuid.UUID, chain_depth: int
) -> RequestContext:
    principal = Principal(
        type="user",
        id=created_by,
        tenant_id=tenant_id,
        permissions=dict.fromkeys(WORKFLOW_PERMISSIONS, Scope.TENANT),
    )
    return RequestContext(
        tenant_id=tenant_id,
        principal=principal,
        actor_kind="workflow",
        actor_id=rule_id,
        workflow_chain_depth=chain_depth,
    )


# ================================================================ snapshots
_applications = table(
    "applications",
    column("id", Uuid()),
    column("demand_id", Uuid()),
    column("candidate_id", Uuid()),
    column("owner_id", Uuid()),
)
_submissions = table(
    "submissions",
    column("id", Uuid()),
    column("application_id", Uuid()),
    column("demand_id", Uuid()),
    column("client_id", Uuid()),
    column("candidate_id", Uuid()),
    column("below_min_margin", Boolean()),
)


async def _demand_facts(ctx: RequestContext, db: AsyncSession, demand_id: Any) -> dict[str, Any]:
    from app.services.demands import workflow_snapshot as demand_snapshot

    facts = await demand_snapshot.workflow_snapshot(ctx, db, uuid.UUID(str(demand_id))) if demand_id else None
    if not facts:
        return {}
    return {
        "demand_id": facts["id"],
        "client_id": facts["client_id"],
        "primary_recruiter_id": facts.get("primary_recruiter_id"),
    }


async def _application_snapshot(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> dict[str, Any] | None:
    row = (
        await db.execute(
            select(_applications.c.demand_id, _applications.c.candidate_id, _applications.c.owner_id).where(
                _applications.c.id == application_id
            )
        )
    ).first()
    if row is None:
        return None
    pipeline = entity_refs.optional_service("app.services.pipeline_service")
    fn = getattr(pipeline, "workflow_snapshot", None) if pipeline is not None else None
    facts: dict[str, Any] = {}
    if fn is not None:
        try:
            facts = dict(await fn(ctx, db, application_id) or {})
        except DomainError:
            return None
    from app.services.candidates import workflow_snapshot as candidate_snapshot

    out = {
        **facts,
        "id": str(application_id),
        "application_id": str(application_id),
        "candidate_id": str(row.candidate_id),
        "owner_id": str(row.owner_id),
        "candidate.tags": await candidate_snapshot.candidate_tag_names(db, row.candidate_id),
    }
    out.update(await _demand_facts(ctx, db, row.demand_id))
    return out


async def _submission_snapshot(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID
) -> dict[str, Any] | None:
    row = (await db.execute(select(_submissions).where(_submissions.c.id == submission_id))).first()
    if row is None:
        return None
    out = {
        "id": str(submission_id),
        "application_id": str(row.application_id),
        "candidate_id": str(row.candidate_id),
        "below_min_margin": bool(row.below_min_margin),
    }
    out.update(await _demand_facts(ctx, db, row.demand_id))
    out["client_id"] = str(row.client_id)
    return out


async def load_snapshot(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> dict[str, Any] | None:
    """Ids / enums of the trigger entity from its owning service (None when missing or unavailable)."""
    try:
        async with db.begin_nested():
            return await _load(ctx, db, entity_type, entity_id)
    except ProgrammingError:  # the owning epic's tables are not migrated yet
        logger.warning("Workflow snapshot for {} unavailable (tables missing)", entity_type)
    return None


async def _load(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> dict[str, Any] | None:
    if entity_type == "demand":
        from app.services.demands import workflow_snapshot as demand_snapshot

        return await demand_snapshot.workflow_snapshot(ctx, db, entity_id)
    if entity_type == "candidate":
        from app.services.candidates import workflow_snapshot as candidate_snapshot

        return await candidate_snapshot.workflow_snapshot(ctx, db, entity_id)
    if entity_type == "application":
        return await _application_snapshot(ctx, db, entity_id)
    if entity_type == "submission":
        return await _submission_snapshot(ctx, db, entity_id)
    # Epic 32: Phase 2 entities expose their snapshot through the catalogue's loader registry.
    from app.services.workflow.catalogue import snapshot_loader

    loader = snapshot_loader(entity_type)
    if loader is None:
        return None
    found = await loader(ctx, db, entity_id)
    return dict(found) if found is not None else None


def _with_extras(snapshot: Mapping[str, Any] | None, extras: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(snapshot or {})
    out.update({f"event.{k}": v for k, v in (extras or {}).items()})
    return out


# ================================================================ loop protection
async def _claim_window(
    db: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID, entity_id: uuid.UUID
) -> bool:
    """True when this rule may fire for this entity now (at most once per entity per minute)."""  # ruff: ignore[non-imperative-mood]
    key = f"wf:{tenant_id}:{rule_id}:{entity_id}"
    try:
        from redis.asyncio import Redis

        s = get_settings()
        client = Redis(
            host=s.redis_host,
            port=s.redis_port,
            db=3,
            password=s.redis_password or None,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        try:
            return bool(await client.set(key, "1", nx=True, ex=RATE_WINDOW_SECONDS))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning(
            "Workflow rate limiter: Redis unavailable ({}); using the executions table", type(exc).__name__
        )
    since = _now() - timedelta(seconds=RATE_WINDOW_SECONDS)
    recent = (
        await db.execute(
            select(WorkflowExecution.id)
            .where(
                WorkflowExecution.rule_id == rule_id,
                WorkflowExecution.entity_id == entity_id,
                WorkflowExecution.mode == "live",
                WorkflowExecution.created_at >= since,
                WorkflowExecution.status.not_in(("skipped_rate_limit", "skipped_loop")),
            )
            .limit(1)
        )
    ).first()
    return recent is None


# ================================================================ actions
async def run_actions(
    ac_base: dict[str, Any], actions: Sequence[Mapping[str, Any]], *, dry: bool
) -> list[dict[str, Any]]:
    db: AsyncSession = ac_base["db"]
    results: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        kind = str(action.get("type"))
        entry: dict[str, Any] = {"index": index, "type": kind}
        # Epic 48 (decision 9): a trigger's ``allowed_actions`` is enforced at run time too.
        from app.services.workflow.registry import disallowed_actions

        if disallowed_actions(str(ac_base.get("trigger") or ""), [kind]):
            entry.update(
                status="error",
                detail="Action not allowed for this trigger",
                error_code="action_not_allowed_for_trigger",
            )
            results.append(entry)
            continue
        try:
            definition, params = parse_params(action)
            ac = ActionContext(params=params, **ac_base)
            if dry:
                entry.update(status="would_run", detail=await definition.dry_run(ac), error_code=None)
                primary = getattr(params, "primary_user_id", None) if kind == "assign" else None
                if primary is not None:  # later actions resolve "primary_recruiter" as if assigned
                    ac_base = {**ac_base, "snapshot": {**ac_base["snapshot"], "primary_recruiter_id": str(primary)}}
            else:
                async with db.begin_nested():
                    detail = await definition.run(ac)
                entry.update(status="ok", detail=detail, error_code=None)
        except ActionError as exc:
            entry.update(status="error", detail=exc.detail, error_code=exc.code)
        except DomainError as exc:
            entry.update(status="error", detail=exc.message, error_code=exc.code)
        except Exception as exc:
            logger.exception("Workflow action {} failed (execution {})", kind, ac_base.get("execution_id"))
            entry.update(status="error", detail="Unexpected error", error_code=type(exc).__name__[:64])
        results.append(entry)
    return results


def _outcome(results: Sequence[Mapping[str, Any]]) -> str:
    errors = sum(1 for r in results if r["status"] == "error")
    if errors == 0:
        return "succeeded"
    return "failed" if errors == len(results) else "partially_failed"


# ================================================================ live
async def _existing(db: AsyncSession, rule_id: uuid.UUID, event_id: uuid.UUID, entity_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(WorkflowExecution.id).where(
                WorkflowExecution.rule_id == rule_id,
                WorkflowExecution.event_id == event_id,
                WorkflowExecution.entity_id == entity_id,
                WorkflowExecution.mode == "live",
            )
        )
    ).first() is not None


async def _run_rule(
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
    tenant_id = rule.tenant_id
    if await _existing(db, rule.id, event_id, entity_id):
        return None
    now = _now()
    execution = WorkflowExecution(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
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
    elif not await _claim_window(db, tenant_id, rule.id, entity_id):
        execution.status = "skipped_rate_limit"
    try:
        async with db.begin_nested():
            db.add(execution)
            await db.flush()
    except IntegrityError:
        return None  # a concurrent run of the same event won the unique key
    if execution.status != "matched":
        execution.finished_at = _now()
        await db.flush()
        return execution.id

    ctx = workflow_context(tenant_id, rule.id, rule.created_by, chain_depth)
    snapshot = await load_snapshot(ctx, db, entity_type, entity_id)
    flat = _with_extras(snapshot, extras)
    matched, trace = conditions.evaluate(rule.conditions, flat, fields_for(rule.trigger))
    if snapshot is None:
        trace.insert(0, {"path": "snapshot", "result": False, "note": "entity snapshot unavailable"})
    execution.condition_trace = trace
    if not matched:
        execution.status, execution.finished_at = "not_matched", _now()
        await db.flush()
        return execution.id

    rule.last_fired_at = now
    ac_base = {
        "ctx": ctx,
        "db": db,
        "tenant_id": tenant_id,
        "rule_id": rule.id,
        "rule_created_by": rule.created_by,
        "trigger": rule.trigger,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "snapshot": flat,
        "execution_id": execution.id,
    }
    results = await run_actions(ac_base, rule.actions or [], dry=False)
    execution.action_results = results
    execution.status = _outcome(results) if results else "succeeded"
    execution.finished_at = _now()
    await db.flush()
    if execution.status in ("failed", "partially_failed"):
        emit(
            db,
            WorkflowExecutionFailed(
                tenant_id=tenant_id, execution_id=execution.id, rule_id=rule.id, status=execution.status
            ),
        )
    return execution.id


async def evaluate_event(
    db: AsyncSession,
    trigger: str,
    entity_type: str,
    entity_id: uuid.UUID,
    event_id: uuid.UUID,
    extras: Mapping[str, Any] | None = None,
    chain_depth: int = 0,
    enqueued_at: datetime | None = None,
) -> list[uuid.UUID]:
    """Run every active rule of ``trigger`` for one event. Each rule commits on its own (its execution row
    together with its action effects), under the rule's workflow context so events emitted by the actions
    carry the chain depth."""
    if trigger not in TRIGGERS:
        logger.warning("Unknown workflow trigger {}", trigger)
        return []
    tenant_id = entity_refs.session_tenant(db)
    rule_ids = list(
        (
            await db.execute(
                select(WorkflowRule.id)
                .where(
                    WorkflowRule.trigger == trigger,
                    WorkflowRule.is_active.is_(True),
                    WorkflowRule.archived_at.is_(None),
                )
                .order_by(WorkflowRule.created_at, WorkflowRule.id)
            )
        ).scalars()
    )
    executed: list[uuid.UUID] = []
    for rule_id in rule_ids:
        rule = await db.get(WorkflowRule, rule_id)
        if rule is None:
            continue
        token = set_context(workflow_context(tenant_id, rule.id, rule.created_by, chain_depth))
        db.sync_session.info[audit.ACTOR_INFO_KEY] = ("workflow", rule.id)
        try:
            # Epic 32 (AD-31): ``graph IS NULL`` rules keep the unchanged P1 path; graphs go to the executor.
            run = _run_rule
            if rule.graph is not None:
                from app.services.workflow import executor

                run = executor.start_run
            execution_id = await run(
                db,
                rule,
                entity_type=entity_type,
                entity_id=entity_id,
                event_id=event_id,
                extras=extras or {},
                chain_depth=chain_depth,
                enqueued_at=enqueued_at,
            )
            await db.commit()  # after-commit dispatch sees the workflow context (chain depth)
            if execution_id is not None:
                executed.append(execution_id)
        except Exception:
            await db.rollback()
            logger.exception("Workflow rule {} failed for {} {}", rule_id, entity_type, entity_id)
        finally:
            db.sync_session.info.pop(audit.ACTOR_INFO_KEY, None)
            reset_context(token)
    return executed


# ================================================================ dry run
async def dry_run(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    rule_id: uuid.UUID | None,
    rule_version: int,
    created_by: uuid.UUID,
    trigger: str,
    rule_conditions: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    entity_id: uuid.UUID,
    extras: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate and resolve without side effects (FR-59). Returns matched, trace, would-run lines and the
    ``mode=dry_run`` execution id (saved rules only)."""
    definition = TRIGGERS[trigger]
    entity_type = definition.entity_type
    tenant_id = entity_refs.session_tenant(db)
    if entity_refs.is_access_checked(entity_type):
        await entity_refs.require_readable(ctx, db, entity_type, entity_id)
    wctx = workflow_context(tenant_id, rule_id, created_by, 0)
    snapshot = await load_snapshot(wctx, db, entity_type, entity_id)
    if snapshot is None:
        raise NotFound("Resource not found")
    flat = _with_extras(snapshot, extras)
    matched, trace = conditions.evaluate(rule_conditions, flat, fields_for(trigger))
    ac_base = {
        "ctx": wctx,
        "db": db,
        "tenant_id": tenant_id,
        "rule_id": rule_id,
        "rule_created_by": created_by,
        "trigger": trigger,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "snapshot": flat,
        "execution_id": None,
    }
    results = await run_actions(ac_base, actions, dry=True)
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
            condition_trace=trace,
            action_results=results,
            enqueued_at=now,
            started_at=now,
            finished_at=now,
        )
        db.add(execution)
        await db.flush()
        execution_id = execution.id
    return {
        "matched": matched,
        "condition_trace": trace,
        "action_results": results,
        "execution_id": execution_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
    }
