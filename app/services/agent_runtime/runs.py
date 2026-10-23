# ruff: file-ignore[line-too-long]
"""Runs: start, resume, cancel, reads, payload reads and purge (Epic 53, spec 0.8 / 0.9, stories 53.2 / 53.6 / 53.9)."""

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.agentic.agents import AgentSpec, Kickoff, TriggerSpec, agent_triggers, get_agent
from app.core.agentic.tools import get_tool
from app.core.agentic.types import FORBIDDEN_ACTIONS
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.events.agents import AgentRunStarted
from app.models.agent_runtime import ACTIVE_RUN_STATUSES, AgentProposal, AgentRun, AgentRunStep, AgentToolCall
from app.models.tasks import TASK_ENTITY_TYPES
from app.schemas.agentic import (
    ModeDecision,
    PayloadItem,
    ProposalSummary,
    RunOut,
    RunPayloadOut,
    RunQuery,
    RunRef,
    RunSummary,
    StepOut,
    ToolCallOut,
    ToolCatalogOut,
)
from app.schemas.tasks import TaskCreate
from app.services import directory_service, entity_refs, tasks_service
from app.services.agent_runtime import payloads, sanitize, state
from app.services.agent_runtime.executor import finalize, queue_for_origin
from app.services.agent_runtime.placeholders import PORTAL_AGENT_KEYS
from app.services.agent_runtime.policy import PolicyInput, resolve
from app.services.agent_runtime.principals import (
    OwnerUnavailable,
    ensure_agent_principal,
    intersect,
    owner_principal,
    portal_principal,
    portal_rights,
)
from app.services.ai import gateway
from app.services.ai.loop import output_schema


@dataclasses.dataclass(frozen=True, slots=True)
class RunStepRequested:
    """Internal (after-commit) request to run a step on a queue; ``agent_runtime.hooks`` enqueues it."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    step_no: int
    queue: str
    countdown: int = 0


@dataclasses.dataclass(frozen=True)
class OwnerRef:
    """Epic 66 (§0.15, AD-89): the owner of a run. ``user`` owners are internal users (``owner_user_id``); portal owners
    (``client_user`` / ``vendor_user`` / ``candidate``) carry their organization."""

    principal_type: Literal["user", "client_user", "vendor_user", "candidate"]
    principal_id: uuid.UUID
    org_kind: Literal["client", "vendor", "worker"] | None = None
    org_id: uuid.UUID | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def request_step(db: AsyncSession, run: AgentRun, step_no: int, countdown: int = 0) -> None:
    emit(db, RunStepRequested(run.tenant_id, run.id, step_no, queue_for_origin(run.origin), countdown))


# ================================================================ start
def _render_kickoff(spec: AgentSpec, kickoff: Kickoff, subject: tuple[str, uuid.UUID] | None) -> str:
    lines = [
        f"Task for the {spec.name} agent.",
        f"Current time (UTC): {_now().isoformat(timespec='minutes')}",
    ]
    if subject is not None:
        lines.append(f"Subject: {subject[0]} {subject[1]}")
    lines.extend(f"{k}: {v}" for k, v in sorted(kickoff.inputs.items()))
    for key, text in sorted(kickoff.untrusted.items()):
        lines.append(gateway.wrap_untrusted(key, text))
    return "\n".join(lines)


def _default_kickoff(
    spec: AgentSpec, subject: tuple[str, uuid.UUID] | None, trigger_key: str | None
) -> Kickoff:
    payload = {
        "subject_type": subject[0] if subject else None,
        "subject_id": str(subject[1]) if subject else None,
        "trigger_ref": trigger_key,
        "trigger_key": trigger_key,
    }
    if spec.kickoff is not None:
        return spec.kickoff(payload)
    return Kickoff(inputs={k: str(v) for k, v in payload.items() if v is not None})


async def _no_owner_task(
    db: AsyncSession, ctx: RequestContext, spec: AgentSpec, subject: tuple[str, uuid.UUID] | None
) -> None:
    """FR-265: an unresolvable owner creates a Task (Tenant Admins) instead of a run."""
    role_id = await directory_service.role_id_by_key(db, "tenant_admin")
    admins = sorted(await directory_service.user_ids_with_role(db, role_id)) if role_id else []
    active = sorted(await entity_refs.active_user_ids(db, admins))
    if not active:
        return
    entity = subject if subject is not None and subject[0] in TASK_ENTITY_TYPES else None
    data = TaskCreate.model_construct(
        title=f"Agent could not run: no owner ({spec.name})"[:200],
        notes="An AI agent was triggered but no Accountable Owner could be resolved for the record. Assign an owner or run it manually.",
        owner_id=active[0], entity_type=entity[0] if entity else None, entity_id=entity[1] if entity else None,  # type: ignore[arg-type]
        due_at=None, due_all_day=False, priority="normal", reminder_at=None,
    )  # fmt: skip
    async with db.begin_nested():
        await tasks_service.create_system_task(
            RequestContext(tenant_id=ctx.tenant_id), db, data, source="agent"
        )


async def _resolve_owner(
    ctx: RequestContext,
    db: AsyncSession,
    spec: AgentSpec,
    trigger: TriggerSpec | None,
    subject: tuple[str, uuid.UUID] | None,
    explicit: uuid.UUID | None,
) -> uuid.UUID | None:
    if explicit is not None:
        return explicit
    for fn in (trigger.owner if trigger is not None else None, spec.owner):
        if fn is not None:
            owner = await fn(ctx, db, subject)
            if owner is not None:
                return owner
    return None


def _write_tools(spec: AgentSpec) -> list[Any]:
    out = []
    for key in spec.tools:
        try:
            tool = get_tool(key)
        except KeyError:
            continue
        if tool.risk != "read":
            out.append(tool)
    return out


async def start_run(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    agent_key: str,
    trigger_kind: str,
    trigger_key: str | None = None,
    subject: tuple[str, uuid.UUID] | None = None,
    kickoff: Kickoff | None = None,
    owner_user_id: uuid.UUID | None = None,
    dedup_key: str | None = None,
    parent_run_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    origin: str | None = None,
    owner: OwnerRef | None = None,
) -> RunRef:
    try:
        spec = get_agent(agent_key)
    except KeyError:
        return RunRef(run_id=None, status="not_started", reason="unknown_agent")
    if spec.placeholder:
        return RunRef(run_id=None, status="not_started", reason="agent_not_available")
    portal_owner = owner is not None and owner.principal_type != "user"
    if (origin or spec.origin) == "portal" and not portal_owner:
        return RunRef(run_id=None, status="not_started", reason="owner_required")  # Epic 66: portal runs need a portal owner
    if owner is not None and not portal_owner and owner_user_id is None:
        owner_user_id = owner.principal_id
    tenant_id = ctx.tenant_id or entity_refs.session_tenant(db)
    gates = await state.gates(db)
    if gates.any_kill:
        return RunRef(run_id=None, status="not_started", reason="kill_switch")
    if not gates.agentic_on or not gates.agent_on(agent_key):
        return RunRef(run_id=None, status="not_started", reason="feature_off")
    config = await state.config_for(db, agent_key)
    if trigger_key and config is not None and (config.triggers_enabled or {}).get(trigger_key) is False:
        return RunRef(run_id=None, status="not_started", reason="trigger_disabled")
    trigger = next((t for t in agent_triggers(agent_key) if t.key == trigger_key), None)
    if portal_owner:
        assert owner is not None
        return await _start_portal_run(
            ctx, db, spec, owner, tenant_id=tenant_id, trigger_kind=trigger_kind, trigger_key=trigger_key,
            subject=subject, kickoff=kickoff, dedup_key=dedup_key, parent_run_id=parent_run_id,
            conversation_id=conversation_id, origin=origin,
        )  # fmt: skip
    owner_id = await _resolve_owner(ctx, db, spec, trigger, subject, owner_user_id)
    owner = None  # from here on: the owner's live Principal
    if owner_id is not None:
        try:
            owner = await owner_principal(db, tenant_id, owner_id)  # type: ignore[assignment]
        except Exception:
            owner = None
    if owner is None:
        await _no_owner_task(db, ctx, spec, subject)
        return RunRef(run_id=None, status="not_started", reason="owner_unavailable")
    effective = Principal(  # type: ignore[unreachable]
        type="agent",
        id=owner.id,
        tenant_id=tenant_id,
        permissions=intersect(owner.permissions, spec.permission_ceiling),
    )
    writes = _write_tools(spec)
    if writes:
        policies = await state.action_policies(db, agent_key)
        allow = state.allow_list(spec, gates.features, owner)
        run_origin = origin or spec.origin
        modes = [
            resolve(PolicyInput(
                global_kill=False, tenant_kill=False, agentic_on=True, agent_on=True, allow_list=allow, tool=t, args=None,
                effective_permissions=effective.permissions,
                action_policy_mode=policies[t.key].mode if t.key in policies else None,
                config_default_mode=config.default_mode if config is not None else None,
                spec_default_mode=spec.default_mode, origin=run_origin,
            )).mode
            for t in writes
        ]  # fmt: skip
        if all(m == "off" for m in modes):
            return RunRef(run_id=None, status="not_started", reason="all_actions_off")
    if dedup_key:
        existing = (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.agent_key == agent_key,
                    AgentRun.dedup_key == dedup_key,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            reason = "attached" if trigger is not None and trigger.on_active == "attach" else "deduplicated"
            return RunRef(run_id=existing.id, status=existing.status, reason=reason)
    if (
        config is not None
        and config.monthly_token_budget is not None
        and trigger_kind in ("event", "schedule")
        and await state.agent_tokens_this_month(db, agent_key) >= config.monthly_token_budget
    ):
        return RunRef(run_id=None, status="not_started", reason="agent_budget_paused")
    held = (
        trigger_kind == "event"
        and gates.provider_down_since is not None
        and (_now() - gates.provider_down_since).total_seconds()
        > get_settings().agent_provider_outage_seconds
    )
    principal_row = await ensure_agent_principal(db, tenant_id, spec)
    limits = state.effective_limits(spec, config)
    kick = kickoff or _default_kickoff(spec, subject, trigger_key)
    candidate_ids = list(kick.candidate_ids)
    if subject is not None and subject[0] == "candidate" and subject[1] not in candidate_ids:
        candidate_ids.insert(0, subject[1])
    starter = actor_from_ctx(ctx)
    run = AgentRun(
        id=uuid.uuid4(), tenant_id=tenant_id, agent_key=agent_key, agent_principal_id=principal_row.id,
        spec_version=spec.version, origin=origin or spec.origin, trigger_kind=trigger_kind, trigger_key=trigger_key,
        dedup_key=dedup_key, owner_user_id=owner.id, started_by_kind=starter.kind, started_by_id=starter.id,
        subject_type=subject[0] if subject else None, subject_id=subject[1] if subject else None,
        candidate_ids=candidate_ids, related_refs=[], conversation_id=conversation_id, parent_run_id=parent_run_id,
        status="queued", model_tier=spec.model_tier, prompt_ref=spec.prompt_id[:120], limits=limits.to_json(),
        next_step_no=1, step_count=0, tool_call_count=0, write_count=0, retry_count=0, input_tokens=0,
        output_tokens=0, cost_micros=0, tainted=bool(kick.untrusted), policy_violations=0,
        deadline_at=_now() + timedelta(seconds=limits.wall_clock_s),
    )  # fmt: skip
    try:
        async with db.begin_nested():
            db.add(run)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.agent_key == agent_key,
                    AgentRun.dedup_key == dedup_key,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return RunRef(run_id=existing.id, status=existing.status, reason="deduplicated")
    text = _render_kickoff(spec, kick, subject)
    payloads.store(
        db,
        run,
        "kickoff",
        [{"role": "user", "content": [{"type": "text", "text": text}]}],
        step_no=1,
        seq=payloads.SEQ_KICKOFF,
    )
    emit(db, AgentRunStarted(
        tenant_id=tenant_id, run_id=run.id, agent_key=agent_key, origin=run.origin, trigger_kind=trigger_kind,
        subject_type=run.subject_type, subject_id=run.subject_id, owner_user_id=owner.id,
    ))  # fmt: skip
    audit.record(
        "agent_run.started", "agent_run", run.id, session=db, agent_key=agent_key, trigger_kind=trigger_kind
    )
    await db.flush()
    if held:
        return RunRef(run_id=run.id, status="queued", reason="provider_outage_hold")
    request_step(db, run, 1)
    return RunRef(run_id=run.id, status="queued", reason=None)


async def _start_portal_run(
    ctx: RequestContext,
    db: AsyncSession,
    spec: AgentSpec,
    owner: OwnerRef,
    *,
    tenant_id: uuid.UUID,
    trigger_kind: str,
    trigger_key: str | None,
    subject: tuple[str, uuid.UUID] | None,
    kickoff: Kickoff | None,
    dedup_key: str | None,
    parent_run_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    origin: str | None,
) -> RunRef:
    """Epic 66 (§0.15): a run owned by a portal principal. Effective Rights = ceiling ∩ ``portal_permissions``; no
    internal owner (``owner_user_id`` NULL) and no fallback Task. The executor opens its tool sessions with the owner's
    org scope. A portal owner always runs with origin ``portal`` (reads execute, writes are Drafts)."""
    del origin
    try:
        portal = await portal_principal(
            db, tenant_id, owner.principal_type, owner.principal_id, owner.org_kind, owner.org_id
        )
    except OwnerUnavailable:
        return RunRef(run_id=None, status="not_started", reason="owner_unavailable")
    if not portal_rights(portal, spec.permission_ceiling) and _write_tools(spec):
        return RunRef(run_id=None, status="not_started", reason="all_actions_off")
    if dedup_key:
        existing = (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.agent_key == spec.key,
                    AgentRun.dedup_key == dedup_key,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return RunRef(run_id=existing.id, status=existing.status, reason="deduplicated")
    config = await state.config_for(db, spec.key)
    principal_row = await ensure_agent_principal(db, tenant_id, spec)
    limits = state.effective_limits(spec, config)
    kick = kickoff or _default_kickoff(spec, subject, trigger_key)
    starter = actor_from_ctx(ctx)
    run = AgentRun(
        id=uuid.uuid4(), tenant_id=tenant_id, agent_key=spec.key, agent_principal_id=principal_row.id,
        spec_version=spec.version, origin="portal", trigger_kind=trigger_kind, trigger_key=trigger_key,
        dedup_key=dedup_key, owner_user_id=None, owner_principal_type=owner.principal_type,
        owner_principal_id=owner.principal_id, owner_org_kind=owner.org_kind, owner_org_id=owner.org_id,
        started_by_kind=starter.kind, started_by_id=starter.id,
        subject_type=subject[0] if subject else None, subject_id=subject[1] if subject else None,
        candidate_ids=list(kick.candidate_ids), related_refs=[], conversation_id=conversation_id,
        parent_run_id=parent_run_id, status="queued", model_tier=spec.model_tier, prompt_ref=spec.prompt_id[:120],
        limits=limits.to_json(), next_step_no=1, step_count=0, tool_call_count=0, write_count=0, retry_count=0,
        input_tokens=0, output_tokens=0, cost_micros=0, tainted=bool(kick.untrusted), policy_violations=0,
        deadline_at=_now() + timedelta(seconds=limits.wall_clock_s),
    )  # fmt: skip
    db.add(run)
    await db.flush()
    payloads.store(
        db,
        run,
        "kickoff",
        [{"role": "user", "content": [{"type": "text", "text": _render_kickoff(spec, kick, subject)}]}],
        step_no=1,
        seq=payloads.SEQ_KICKOFF,
    )
    audit.record(
        "agent_run.started", "agent_run", run.id, session=db, agent_key=spec.key, trigger_kind=trigger_kind,
        owner_principal_type=owner.principal_type,
    )  # fmt: skip
    await db.flush()
    request_step(db, run, 1)
    return RunRef(run_id=run.id, status="queued", reason=None)


# ================================================================ waits
async def resume(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    condition: str,
    ref: uuid.UUID,
    payload: Mapping[str, Any] | None = None,
    untrusted_text: str | None = None,
) -> list[RunRef]:
    rows = list(
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.status == "waiting",
                    AgentRun.wait_condition == condition,
                    AgentRun.wait_ref == ref,
                )
                .with_for_update()
            )
        ).scalars()
    )
    out: list[RunRef] = []
    for run in rows:
        parts = [f"Event: {condition} ({ref})"]
        if payload:
            import json

            parts.append(json.dumps(dict(payload), sort_keys=True, default=str))
        if untrusted_text:
            parts.append(gateway.wrap_untrusted(f"{condition}:{ref}", untrusted_text))
            run.tainted = True
            if await sanitize.prescreen(ctx, db, untrusted_text) == "suspicious":
                run.limits = {**(run.limits or {}), "suspicious": 1}
        delta = [{"role": "user", "content": [{"type": "text", "text": "\n".join(parts)}]}]
        payloads.store(db, run, "request_delta", delta, step_no=run.next_step_no, seq=payloads.SEQ_DELTA)
        run.status = "queued"
        run.wait_condition, run.wait_ref, run.wait_until = None, None, None
        request_step(db, run, run.next_step_no)
        out.append(RunRef(run_id=run.id, status="queued", reason=None))
    await db.flush()
    return out


# ================================================================ cancel / reads
def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal):
        raise NotFound("Run not found")
    return principal


async def _run(db: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> AgentRun:
    stmt = select(AgentRun).where(AgentRun.id == run_id)
    if lock:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise NotFound("Run not found")
    return run


async def _can_view(ctx: RequestContext, db: AsyncSession, principal: Principal, run: AgentRun) -> bool:
    if principal.has("ai:govern") or run.owner_user_id == principal.id:
        return True
    if run.origin == "copilot":
        return False
    if run.subject_type and run.subject_id:
        return await entity_refs.can_read(ctx, db, run.subject_type, run.subject_id)
    return False


CANCEL_REASON_CODES = ("manual", "alternative_process", "subject_closed")


async def cancel_run(
    ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, *, reason: str, reason_code: str = "manual"
) -> RunRef:
    """Cancel an active run. ``reason_code``: ``manual`` (default), ``alternative_process`` (the person handles it
    another way, C-18) or ``subject_closed``. System contexts (no principal) may cancel too."""
    if reason_code not in CANCEL_REASON_CODES:
        raise ValidationFailed("Unknown cancel reason", code="invalid_reason_code")
    principal = ctx.principal if isinstance(ctx.principal, Principal) else None
    run = await _run(db, run_id, lock=True)
    if principal is not None and not (principal.has("ai:govern") or run.owner_user_id == principal.id):
        raise NotFound("Run not found")
    if run.status in ACTIVE_RUN_STATUSES:
        await finalize(db, run, "cancelled", reason_code, run_fallback=False)
        audit.record("agent_run.cancelled", "agent_run", run.id, session=db, reason=reason[:500])
    return RunRef(run_id=run.id, status=run.status, reason=run.reason_code)


def _label(spec_name: str, owner_name: str | None) -> str:
    return f"{spec_name} for {owner_name}" if owner_name else spec_name


def _agent_name(key: str) -> str:
    try:
        return get_agent(key).name
    except KeyError:
        return key


def _summary(run: AgentRun, owner_name: str | None) -> dict[str, Any]:
    name = _agent_name(run.agent_key)
    queue_age = None
    if run.status == "queued":
        queue_age = int((_now() - run.created_at).total_seconds()) if run.created_at else 0
    return {
        "id": run.id, "agent_key": run.agent_key, "agent_name": name, "origin": run.origin,
        "trigger_kind": run.trigger_kind, "trigger_key": run.trigger_key, "status": run.status,
        "reason_code": run.reason_code, "subject_type": run.subject_type, "subject_id": run.subject_id,
        "owner_user_id": run.owner_user_id, "actor_label": _label(name, owner_name), "step_count": run.step_count,
        "tool_call_count": run.tool_call_count, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "cost_micros": run.cost_micros, "tainted": run.tainted, "policy_violations": run.policy_violations,
        "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
        "queue_age_seconds": queue_age,
    }  # fmt: skip


async def get_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RunOut:
    principal = _principal(ctx)
    run = await _run(db, run_id)
    if not await _can_view(ctx, db, principal, run):
        raise NotFound("Run not found")
    names = await directory_service.user_names(db, [run.owner_user_id])  # type: ignore[list-item]
    steps = (
        await db.execute(
            select(AgentRunStep).where(AgentRunStep.run_id == run.id).order_by(AgentRunStep.step_no)
        )
    ).scalars()
    calls = (
        await db.execute(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run.id)
            .order_by(AgentToolCall.step_no, AgentToolCall.created_at)
        )
    ).scalars()
    props = (
        await db.execute(
            select(AgentProposal).where(AgentProposal.run_id == run.id).order_by(AgentProposal.created_at)
        )
    ).scalars()
    return RunOut(
        **_summary(run, names.get(run.owner_user_id)),  # type: ignore[arg-type]
        related_refs=list(run.related_refs or []),
        candidate_ids=list(run.candidate_ids or []),
        limits=dict(run.limits or {}),
        wait_condition=run.wait_condition,
        wait_until=run.wait_until,
        result=run.result,
        fallback_task_id=run.fallback_task_id,
        steps=[
            StepOut(
                step_no=s.step_no,
                status=s.status,
                stop_reason=s.stop_reason,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                latency_ms=s.latency_ms,
                ai_action_id=s.ai_action_id,
            )
            for s in steps
        ],
        tool_calls=[
            ToolCallOut(
                id=c.id,
                step_no=c.step_no,
                tool_key=c.tool_key,
                tool_version=c.tool_version,
                risk=c.risk,
                mode=c.mode,
                mode_reason=c.mode_reason,
                outcome=c.outcome,
                error_code=c.error_code,
                proposal_id=c.proposal_id,
                egress=c.egress,
                target_type=c.target_type,
                target_id=c.target_id,
                # C-14: Copilot tool arguments are private to the user (admins see metadata only).
                args_redacted=c.args_redacted
                if run.origin != "copilot" or run.owner_user_id == principal.id
                else None,
                result_summary=c.result_summary,
                latency_ms=c.latency_ms,
            )
            for c in calls
        ],
        proposals=[
            ProposalSummary(
                id=p.id, human_id=p.human_id, tool_key=p.tool_key, title=p.title, status=p.status, risk=p.risk
            )
            for p in props
            if p.origin != "copilot" or p.owner_user_id == principal.id
        ],
    )


async def list_runs(ctx: RequestContext, db: AsyncSession, q: RunQuery) -> Page[RunSummary]:
    principal = _principal(ctx)
    stmt = select(AgentRun)
    if not principal.has("ai:govern"):
        if (
            q.subject_type
            and q.subject_id
            and await entity_refs.can_read(ctx, db, q.subject_type, q.subject_id)
        ):
            stmt = stmt.where(or_(AgentRun.origin != "copilot", AgentRun.owner_user_id == principal.id))
        else:
            stmt = stmt.where(AgentRun.owner_user_id == principal.id)
    for column, value in (
        (AgentRun.agent_key, q.agent_key), (AgentRun.status, q.status), (AgentRun.origin, q.origin),
        (AgentRun.subject_type, q.subject_type), (AgentRun.subject_id, q.subject_id), (AgentRun.owner_user_id, q.owner_user_id),
    ):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    if q.created_from:
        stmt = stmt.where(AgentRun.created_at >= q.created_from)
    if q.created_to:
        stmt = stmt.where(AgentRun.created_at < q.created_to)
    stmt = stmt.order_by(AgentRun.created_at.desc(), AgentRun.id)
    page = await paginate(db, stmt, PageParams(page=q.page, page_size=q.page_size))
    names = await directory_service.user_names(db, {r.owner_user_id for r in page.items})
    return Page[RunSummary](
        items=[RunSummary(**_summary(r, names.get(r.owner_user_id))) for r in page.items],
        total=page.total, page=page.page, page_size=page.page_size,
    )  # fmt: skip


async def read_payload(
    ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, *, reason: str
) -> RunPayloadOut:
    principal = _principal(ctx)
    if not principal.has("ai:payload_read"):  # C-14: ai:govern alone never returns content
        raise NotFound("Run not found")
    if len(reason.strip()) < 10:
        raise ValidationFailed("Give a reason of at least 10 characters", code="reason_required")
    run = await _run(db, run_id)
    if run.origin == "copilot" or run.agent_key in PORTAL_AGENT_KEYS:  # Epic 66: portal Copilots too (AD-64)
        raise Forbidden("Copilot conversations are private to their user", code="copilot_private")
    rows = await payloads.rows_for(db, run.id)
    audit.record(
        "agent_run.payload_read",
        "agent_run",
        run.id,
        session=db,
        reason=reason.strip()[:500],
        items=len(rows),
    )
    return RunPayloadOut(
        run_id=run.id,
        items=[
            PayloadItem(
                kind=r.kind,
                step_no=r.step_no,
                tool_call_id=r.tool_call_id,
                content=payloads.decrypt(r),
                sha256=r.sha256,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


async def purge_run_content(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> int:
    """C-2: erase the stored prompts / outputs (payloads, redacted args, summaries, result) of a run; keep metadata.
    Allowed for the run owner, ``ai:govern`` or a system context (no principal: erasure / retention jobs)."""
    principal = ctx.principal
    run = await _run(db, run_id, lock=True)
    if isinstance(principal, Principal) and not (
        principal.has("ai:govern") or run.owner_user_id == principal.id
    ):
        raise NotFound("Run not found")
    removed = await payloads.purge_run(db, run.id)
    await db.execute(
        update(AgentToolCall)
        .where(AgentToolCall.run_id == run.id)
        .values(args_redacted=None, result_summary=None)
    )
    run.result = None
    audit.record("agent_run.content_purged", "agent_run", run.id, session=db, payloads=removed)
    await db.flush()
    return removed


# ================================================================ policy probe / catalogue
async def effective_mode(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    agent_key: str,
    tool_key: str,
    args: Mapping[str, Any] | None = None,
) -> ModeDecision:
    principal = _principal(ctx)
    try:
        spec = get_agent(agent_key)
        tool = get_tool(tool_key)
    except KeyError as exc:
        raise NotFound("Unknown agent or tool") from exc
    gates = await state.gates(db)
    config = await state.config_for(db, agent_key)
    policies = await state.action_policies(db, agent_key)
    parsed = tool.input_model.model_validate(dict(args)) if args is not None else None
    return resolve(PolicyInput(
        global_kill=gates.global_kill, tenant_kill=gates.tenant_kill or gates.platform_tenant_kill,
        agentic_on=gates.agentic_on, agent_on=gates.agent_on(agent_key), allow_list=state.allow_list(spec, gates.features, principal),
        tool=tool, args=parsed, effective_permissions=intersect(principal.permissions, spec.permission_ceiling),
        action_policy_mode=policies[tool_key].mode if tool_key in policies else None,  # type: ignore[arg-type]
        config_default_mode=config.default_mode if config is not None else None,  # type: ignore[arg-type]
        spec_default_mode=spec.default_mode, origin=spec.origin,
        burn_in_until=config.burn_in_until if config is not None else None, now=_now(),
    ))  # fmt: skip


def tool_catalogue() -> list[ToolCatalogOut]:
    from app.core.agentic.tools import tools

    out = [
        ToolCatalogOut(
            key=t.key, version=t.version, owner_epic=t.owner_epic, title=t.title, description=t.description, risk=t.risk,
            permissions=list(t.permissions), egress=t.egress, resource=t.scope.resource, default_mode=t.default_mode,
            input_schema=output_schema(t.input_model),
        )
        for t in sorted(tools().values(), key=lambda t: t.key)
    ]  # fmt: skip
    out.extend(
        ToolCatalogOut(
            key=f"{entity}.{target}", version=1, owner_epic="53", title=f"{entity.replace('_', ' ')} {target.replace('_', ' ')}",
            description="A human-only (★) action. AI agents can never perform it; they may only recommend it.",
            risk="forbidden", permissions=[], egress=False, resource=entity, default_mode=None, input_schema={}, forbidden=True,
        )
        for entity, target in FORBIDDEN_ACTIONS
    )  # fmt: skip
    return out


__all__ = [
    "OwnerRef",
    "RunStepRequested",
    "cancel_run",
    "effective_mode",
    "get_run",
    "list_runs",
    "purge_run_content",
    "read_payload",
    "request_step",
    "resume",
    "start_run",
    "tool_catalogue",
]
