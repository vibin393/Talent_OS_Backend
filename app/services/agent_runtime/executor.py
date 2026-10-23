# ruff: file-ignore[line-too-long]
"""Step executor (Epic 53, AD-58, stories 53.4 / 53.5 / 53.7 / 53.8 / 53.9). One Celery task per step.

``run_step(tenant_id, run_id, step_no, worker_id)``:

* **Tx A (claim).** Lock the run; exit unless ``queued|running``, ``next_step_no == step_no`` and no live foreign lease.
  Gates (kill switches, features, owner, step / token caps, agent budget, deadline) end the run. Take the semaphores
  (else retry in 10 s) and the lease; rebuild the history from ``agent_payloads``; compute the offered tools; commit.
* **Model call** (``ai.run_agent_step``) in its own session -- no transaction is open across it.
* **Tx B (apply).** Re-lock; a lost lease / moved step drops the result (a ``dropped`` step row). Insert the step, the
  payloads and the counters; map model errors; execute / propose / refuse every tool use in order (SAVEPOINT each),
  logging one ``agent_tool_calls`` row per tool use; append one user message with all ``tool_result`` blocks; advance.

Returns ``StepOutcome`` -- the task enqueues the next step (or a retry) from it.
"""

import asyncio
import dataclasses
import json
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.agentic.agents import AgentSpec, EmitResult, get_agent
from app.core.agentic.exposure import get_exposure
from app.core.agentic.tools import RUNTIME_RESERVED_FIELDS, get_tool, get_tool_by_wire
from app.core.agentic.types import WAIT_CONDITION_RE, RunLimits, ToolOutcome, ToolSpec
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import OrgScope, tenant_session
from app.core.errors import ConflictError, DomainError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Principal
from app.events.agents import AgentRunFailed, AgentRunFinished
from app.models.agent_runtime import AgentRun, AgentRunStep, AgentToolCall
from app.schemas.agentic import ModelToolDef, StepRequest, StepResult
from app.services import ai
from app.services.agent_runtime import fallback, locks, payloads, proposals, send_policy, state
from app.services.agent_runtime.policy import PolicyInput, resolve
from app.services.agent_runtime.principals import OwnerUnavailable, effective_principal, tool_context_request
from app.services.agent_runtime.sanitize import redact_for_log
from app.services.ai.loop import output_schema, tool_result_block, wrap_tool_result

LEASE_GRACE_SECONDS = 30
RETRY_BACKOFF = (10, 60)
SEMAPHORE_RETRY_SECONDS = 10
WARNING_TEXT = "Limit nearly reached: finish now with emit_result and summarise what remains."
MAX_RELATED_REFS = 200
POLICY_VIOLATION_LIMIT = 3


@dataclasses.dataclass(frozen=True, slots=True)
class StepOutcome:
    next_step: int | None = None
    countdown: int | None = None
    status: str | None = None
    note: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class ExternalCall:
    """Epic 66 (§0.15): the per-call inputs of an ``origin='external'`` (MCP) tool call -- the delegated token's
    scopes, the tenant's enabled MCP tool groups and the Proposal label ("Requested by <client> for <User> through
    MCP")."""

    oauth_scopes: frozenset[str]
    groups_enabled: frozenset[str]
    proposal_label: str = ""
    app_installation_id: uuid.UUID | None = None


def proposal_origin(run_origin: str) -> str:
    """Return the ``agent_proposals.origin`` of a Proposal made by a run of ``run_origin``."""
    if run_origin in ("copilot", "external", "portal"):
        return run_origin
    return "agent"


def is_portal_owned(run: AgentRun) -> bool:
    return run.owner_principal_type not in (None, "user") and bool(run.owner_org_kind and run.owner_org_id)


class AwaitEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=3, max_length=40)
    ref: uuid.UUID | None = None
    hours: int = Field(ge=1, le=168)
    reason: str = Field(default="", max_length=200)


def _now() -> datetime:
    return datetime.now(UTC)


def queue_for_origin(origin: str) -> str:
    return "conversations" if origin in ("copilot", "conversation", "portal") else "agents"  # R-5: portal Copilots


def _subject_key(run: AgentRun) -> str:
    return f"{run.subject_type}:{run.subject_id}" if run.subject_id else "-"


def _limits(run: AgentRun) -> RunLimits:
    known = {k: int(v) for k, v in (run.limits or {}).items() if k in RunLimits.__dataclass_fields__}
    return RunLimits(**known)


def _consent_subject(run: AgentRun) -> tuple[str, uuid.UUID] | None:
    if run.subject_type == "candidate" and run.subject_id:
        return ("candidate", run.subject_id)
    if run.candidate_ids:
        return ("candidate", run.candidate_ids[0])
    if run.subject_type and run.subject_id:
        return (run.subject_type, run.subject_id)
    return None


def _system_prompt(spec: AgentSpec) -> str:
    parts: list[str] = []
    for prompt_id in ("agent_base_policy", spec.prompt_id):
        try:
            parts.append(ai.load_prompt(prompt_id).system)
        except (FileNotFoundError, ValueError):
            if prompt_id == spec.prompt_id:
                parts.append(f"You are the {spec.name} agent. Purpose: {spec.purpose}")
    return "\n\n".join(parts)


def _with_reserved(schema: dict[str, Any]) -> dict[str, Any]:
    props = dict(schema.get("properties") or {})
    props["_rationale"] = {
        "type": "string",
        "maxLength": 1000,
        "description": "Why you propose this (shown to the reviewer).",
    }
    props["_confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    props["_evidence"] = {
        "type": "array",
        "maxItems": 10,
        "items": {
            "type": "object",
            "properties": {"type": {"type": "string"}, "id": {"type": "string"}},
            "required": ["type", "id"],
            "additionalProperties": False,
        },
    }
    return {**schema, "properties": props}


def tool_defs(spec: AgentSpec, offered: list[ToolSpec], limits: RunLimits) -> tuple[ModelToolDef, ...]:
    defs = [
        ModelToolDef(
            name=t.wire_name,
            description=t.description,
            input_schema=output_schema(t.input_model)
            if t.risk == "read"
            else _with_reserved(output_schema(t.input_model)),
        )
        for t in offered
    ]
    defs.append(
        ModelToolDef(
            name="emit_result",
            description="Finish the task: return the result. Call it exactly once, last.",
            input_schema=output_schema(spec.result_model or EmitResult),
        )
    )
    if limits.max_wait_hours > 0:
        defs.append(
            ModelToolDef(
                name="await_event",
                description="Pause until an event happens (a reply, a slot pick or a proposal decision), at most the given hours.",
                input_schema=output_schema(AwaitEventIn),
            )
        )
    return tuple(sorted(defs, key=lambda d: d.name))


async def _offered_tools(
    db: AsyncSession, run: AgentRun, spec: AgentSpec, gates: state.Gates, principal: Principal
) -> tuple[list[ToolSpec], frozenset[str]]:
    config = await state.config_for(db, spec.key)
    policies = await state.action_policies(db, spec.key)
    allow = state.allow_list(spec, gates.features, principal)
    offered: list[ToolSpec] = []
    for key in sorted(allow):
        try:
            tool = get_tool(key)
        except KeyError:
            continue
        decision = resolve(_policy_input(run, spec, tool, None, gates, principal, allow, config, policies))
        if decision.mode != "off":
            offered.append(tool)
    return offered, allow


def _policy_input(
    run: AgentRun,
    spec: AgentSpec,
    tool: ToolSpec,
    args: Any,
    gates: state.Gates,
    principal: Principal,
    allow: frozenset[str],
    config: Any,
    policies: Mapping[str, Any],
    *,
    send_intent: Any = None,
    grounded_ok: bool = False,
    external: ExternalCall | None = None,
) -> PolicyInput:
    policy = policies.get(tool.key)
    if external is not None:
        return dataclasses.replace(
            _policy_input(run, spec, tool, args, gates, principal, allow, config, policies, send_intent=send_intent, grounded_ok=grounded_ok),
            run_tainted=True, exposure=get_exposure(tool.key), token_scopes=external.oauth_scopes,
            mcp_groups_enabled=external.groups_enabled,
        )  # fmt: skip
    return PolicyInput(
        global_kill=gates.global_kill,
        tenant_kill=gates.tenant_kill or gates.platform_tenant_kill,
        agentic_on=gates.agentic_on,
        agent_on=gates.agent_on(spec.key),
        allow_list=allow,
        tool=tool,
        args=args,
        effective_permissions=principal.permissions,
        action_policy_mode=policy.mode if policy is not None else None,
        config_default_mode=config.default_mode if config is not None else None,
        spec_default_mode=spec.default_mode,
        origin=run.origin,
        run_tainted=run.tainted,
        suspicious=bool((run.limits or {}).get("suspicious")),
        burn_in_until=config.burn_in_until if config is not None else None,
        now=_now(),
        send_intent=send_intent,
        grounded_ok=grounded_ok,
    )


# ================================================================ finishing
async def finalize(
    db: AsyncSession,
    run: AgentRun,
    status: str,
    reason: str | None,
    *,
    spec: AgentSpec | None = None,
    run_fallback: bool | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """End a run (caller commits). Fallback Task for failed / stopped runs unless the kill switch or a manual cancel."""
    run.status = status
    run.reason_code = reason
    run.finished_at = _now()
    run.lease_owner = None
    run.lease_expires_at = None
    run.wait_condition = None
    run.wait_ref = None
    run.wait_until = None
    if result is not None:
        run.result = result
    if spec is None:
        try:
            spec = get_agent(run.agent_key)
        except KeyError:
            spec = None
    wants_fallback = (
        run_fallback
        if run_fallback is not None
        else (status in ("failed", "stopped") and reason not in ("kill_switch", "manual"))
    )
    if wants_fallback and spec is not None and run.owner_user_id is not None:
        await fallback.create_task(db, run, spec, reason or status)
    if status == "failed":
        emit(db, AgentRunFailed(
            tenant_id=run.tenant_id, run_id=run.id, agent_key=run.agent_key, reason_code=reason or "failed",
            subject_type=run.subject_type, subject_id=run.subject_id, owner_user_id=run.owner_user_id,  # type: ignore[arg-type]
            fallback_task_id=run.fallback_task_id,
        ))  # fmt: skip
    else:
        emit(db, AgentRunFinished(
            tenant_id=run.tenant_id, run_id=run.id, agent_key=run.agent_key, status=status, reason_code=reason,
            subject_type=run.subject_type, subject_id=run.subject_id, owner_user_id=run.owner_user_id,  # type: ignore[arg-type]
        ))  # fmt: skip
    audit.record(
        "agent_run.finished", "agent_run", run.id, session=db, agent_key=run.agent_key, status=status,
        reason_code=reason, steps=run.step_count, tool_calls=run.tool_call_count,
    )  # fmt: skip
    await db.flush()
    await locks.release(run.tenant_id, run.agent_key, _subject_key(run), run.id)


# ================================================================ tx A
@dataclasses.dataclass
class _Claim:
    request: StepRequest
    ctx: RequestContext
    warning: list[dict[str, Any]]
    spec: AgentSpec


async def _claim(
    tenant_id: uuid.UUID, run_id: uuid.UUID, step_no: int, worker_id: str, max_tokens: int
) -> _Claim | StepOutcome:
    async with tenant_session(tenant_id) as db:
        run = (
            await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None or run.status not in ("queued", "running") or run.next_step_no != step_no:
            return StepOutcome(note="not_claimable")
        now = _now()
        if (
            run.lease_owner
            and run.lease_owner != worker_id
            and run.lease_expires_at
            and run.lease_expires_at > now
        ):
            return StepOutcome(note="leased")
        try:
            spec = get_agent(run.agent_key)
        except KeyError:
            await finalize(db, run, "failed", "manual", run_fallback=False)
            return StepOutcome(status="failed")
        gates = await state.gates(db)
        if gates.any_kill:
            await finalize(
                db, run, "cancelled" if run.status == "queued" else "stopped", "kill_switch", spec=spec
            )
            return StepOutcome(status=run.status)
        if not gates.agentic_on or not gates.agent_on(spec.key):
            await finalize(db, run, "cancelled", "manual", spec=spec, run_fallback=False)
            return StepOutcome(status="cancelled")
        try:
            principal = await effective_principal(db, run, spec)
        except OwnerUnavailable:
            await finalize(db, run, "failed", "owner_unavailable", spec=spec)
            return StepOutcome(status="failed")
        limits = _limits(run)
        if run.step_count >= limits.max_steps:
            await finalize(db, run, "stopped", "cap_steps", spec=spec)
            return StepOutcome(status="stopped")
        if run.input_tokens + run.output_tokens >= limits.max_tokens_per_run:
            await finalize(db, run, "stopped", "cap_tokens", spec=spec)
            return StepOutcome(status="stopped")
        config = await state.config_for(db, spec.key)
        if (
            config is not None
            and config.monthly_token_budget is not None
            and run.trigger_kind in ("event", "schedule")
            and await state.agent_tokens_this_month(db, spec.key) >= config.monthly_token_budget
        ):
            await finalize(db, run, "stopped", "budget", spec=spec)
            return StepOutcome(status="stopped")
        if now > run.deadline_at:
            await finalize(db, run, "stopped", "timeout", spec=spec)
            return StepOutcome(status="stopped")
        lease_s = limits.step_timeout_s + LEASE_GRACE_SECONDS
        if not await locks.acquire(run.tenant_id, run.agent_key, _subject_key(run), run.id, lease_s + 60):
            return StepOutcome(next_step=step_no, countdown=SEMAPHORE_RETRY_SECONDS, note="semaphore")
        run.status = "running"
        run.started_at = run.started_at or now
        run.lease_owner = worker_id
        run.lease_expires_at = now + timedelta(seconds=lease_s)
        history = await payloads.history(db, run.id, before_step=step_no)
        warning: list[dict[str, Any]] = []
        ratios = (
            run.step_count / limits.max_steps,
            run.tool_call_count / limits.max_tool_calls,
            run.write_count / limits.max_writes,
            (run.input_tokens + run.output_tokens) / limits.max_tokens_per_run,
            (now - (run.started_at or now)).total_seconds() / limits.wall_clock_s,
        )
        if max(ratios) >= 0.8:
            warning = [{"role": "system", "content": WARNING_TEXT}]
        offered, _ = await _offered_tools(db, run, spec, gates, principal)
        request = StepRequest(
            agent_key=spec.key,
            run_id=run.id,
            step_no=step_no,
            model_tier=spec.model_tier,
            effort=spec.effort,
            system=_system_prompt(spec),
            tools=tool_defs(spec, offered, limits),
            messages=tuple(history + warning),
            max_tokens=max_tokens,
            subject=_consent_subject(run),
            user_started=run.trigger_kind in ("manual", "copilot"),
        )
        ctx = tool_context_request(run, principal, step_no)
    return _Claim(request=request, ctx=ctx, warning=warning, spec=spec)


# ================================================================ tx B helpers
@dataclasses.dataclass
class _Call:
    tool_use_id: str
    name: str
    raw: dict[str, Any]
    tool: ToolSpec | None = None
    outcome: str = "invalid"
    mode: str = "off"
    mode_reason: str = "policy"
    risk: str = "high"
    error_code: str | None = None
    proposal_id: uuid.UUID | None = None
    result: dict[str, Any] | None = None
    result_text: str = ""
    summary: str = ""
    target: tuple[str, uuid.UUID] | None = None
    candidate_ids: tuple[uuid.UUID, ...] = ()
    idempotency_key: str = ""
    latency_ms: int | None = None
    args_json: dict[str, Any] | None = None
    executed_as: dict[str, Any] | None = None


def _error_text(code: str, detail: Mapping[str, Any] | None = None) -> str:
    return json.dumps({"error": code, **(detail or {})}, sort_keys=True, default=str)


def _related_ids(run: AgentRun) -> set[str]:
    ids = {str(ref[1]) for ref in run.related_refs or [] if isinstance(ref, list | tuple) and len(ref) == 2}
    ids.update(str(c) for c in run.candidate_ids or [])
    if run.subject_id:
        ids.add(str(run.subject_id))
    return ids


def _add_refs(run: AgentRun, outcome: ToolOutcome) -> None:
    refs = [list(r) for r in run.related_refs or []]
    seen = {(str(r[0]), str(r[1])) for r in refs if len(r) == 2}
    for kind, rid in outcome.refs:
        if (kind, str(rid)) not in seen and len(refs) < MAX_RELATED_REFS:
            refs.append([kind, str(rid)])
            seen.add((kind, str(rid)))
    run.related_refs = refs
    cands = list(run.candidate_ids or [])
    for cid in outcome.candidate_ids:
        if cid not in cands:
            cands.append(cid)
    run.candidate_ids = cands


def _untrusted_present(data: Mapping[str, Any], paths: frozenset[str]) -> bool:
    def walk(node: Any, path: str) -> bool:
        if isinstance(node, Mapping):
            return any(walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items())
        if isinstance(node, list):
            return any(walk(v, path) for v in node)
        return path in paths and isinstance(node, str) and bool(node.strip())

    return walk(dict(data), "")


def _idem_key(
    tool: ToolSpec, run: AgentRun, step_no: int, args: BaseModel, target: tuple[str, uuid.UUID] | None
) -> str:
    data = args.model_dump(mode="json")
    fields = {f: data.get(f) for f in tool.idempotency.fields} if tool.idempotency.fields else data
    digest = proposals.sha(fields)
    target_s = f"{target[0]}:{target[1]}" if target else "-"
    if tool.idempotency.per_run:
        return f"{tool.key}:{run.id}:{step_no}:{target_s}:{digest[:16]}"[:200]
    return f"{tool.key}:{target_s}:{digest}"[:200]


async def _execute_tool(
    db: AsyncSession,
    run: AgentRun,
    call: _Call,
    args: BaseModel,
    ctx: RequestContext,
    step_no: int,  # ruff: ignore[unused-function-argument] - kept for log context
) -> None:
    tool = call.tool
    assert tool is not None
    tctx = state.ToolCtx(
        request=ctx, db=db, actor=ai_actor(run), run_id=run.id, agent_key=run.agent_key,
        owner_user_id=run.owner_user_id, subject=(run.subject_type, run.subject_id) if run.subject_id else None,  # type: ignore[arg-type]
        idempotency_key=call.idempotency_key, execution="agent",
    )  # fmt: skip
    started = time.perf_counter()
    token = set_context(ctx)
    try:
        if is_portal_owned(run):  # Epic 66: the portal owner's org scope (org RLS) for the tool's reads
            org = OrgScope.of(str(run.owner_org_kind), run.owner_org_id)  # type: ignore[arg-type]
            async with tenant_session(run.tenant_id, org=org) as tool_db:
                tctx.db = tool_db
                outcome = await asyncio.wait_for(tool.handler(tctx, args), timeout=tool.timeout_s)
        else:
            async with db.begin_nested():
                outcome = await asyncio.wait_for(tool.handler(tctx, args), timeout=tool.timeout_s)
        data = outcome.data.model_dump(mode="json")
        call.outcome = "executed"
        call.result = data
        call.summary = outcome.summary or f"{tool.key} executed"
        call.target = outcome.target or call.target
        call.candidate_ids = outcome.candidate_ids
        call.result_text = wrap_tool_result(data, tool.untrusted_output, source=tool.key)
        _add_refs(run, outcome)
        if call.risk != "read":
            run.write_count += 1
        if tool.untrusted_output and _untrusted_present(data, tool.untrusted_output):
            run.tainted = True
    except NotFound:
        call.outcome, call.error_code = "not_found", "not_found"
    except Forbidden:
        call.outcome, call.error_code = "refused", "forbidden"
    except ValidationFailed as exc:
        call.outcome, call.error_code = "invalid", exc.code[:50]
    except ConflictError as exc:
        call.outcome, call.error_code = "failed", exc.code[:50]
    except DomainError as exc:
        call.outcome, call.error_code = "failed", exc.code[:50]
    except TimeoutError:
        call.outcome, call.error_code = "failed", "tool_timeout"
    except NotImplementedError:
        call.outcome, call.error_code = "failed", "not_available"
    except Exception:
        logger.exception("Agent tool {} failed (run {})", tool.key, run.id)
        call.outcome, call.error_code = "failed", "tool_error"
    finally:
        reset_context(token)
    call.latency_ms = int((time.perf_counter() - started) * 1000)
    call.executed_as = ai_actor(run).to_json()


def ai_actor(run: AgentRun) -> Any:
    from app.core.actors import ActorRef

    return ActorRef("agent", run.agent_principal_id, on_behalf_of=run.owner_user_id)


async def _tool_calls_of(db: AsyncSession, run_id: uuid.UUID, tool_key: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(AgentToolCall)
                .where(AgentToolCall.run_id == run_id, AgentToolCall.tool_key == tool_key)
            )
        ).scalar_one()
    )


async def _process_call(
    db: AsyncSession,
    run: AgentRun,
    spec: AgentSpec,
    call: _Call,
    *,
    step_no: int,
    gates: state.Gates,
    allow: frozenset[str],
    config: Any,
    policies: Mapping[str, Any],
    counts: dict[str, int],
    ctx: RequestContext,
    external: ExternalCall | None = None,
) -> str | None:
    """Process one non-injected tool use. Returns a run-ending reason or None. ``external`` (Epic 66): an MCP call --
    delegated rights, the exposure policy branch and labelled Proposals; a single policy violation does not end the
    session (the limit does)."""
    limits = _limits(run)
    tool = get_tool_by_wire(call.name)
    call.idempotency_key = f"call:{run.id}:{call.tool_use_id}"
    if tool is None or tool.key not in allow:
        call.outcome, call.error_code, call.mode_reason = (
            "policy_violation",
            "not_allowlisted",
            "not_allowlisted",
        )
        call.result_text = _error_text("not_allowlisted", {"message": "This action is not available to you."})
        run.policy_violations += 1
        return _violation_end(run, external)
    call.tool = tool
    raw = {k: v for k, v in call.raw.items() if k not in RUNTIME_RESERVED_FIELDS}
    reserved = {k: v for k, v in call.raw.items() if k in RUNTIME_RESERVED_FIELDS}
    try:
        args = tool.input_model.model_validate(raw)
    except ValidationError as exc:
        call.outcome, call.error_code = "invalid", "invalid_args"
        call.result_text = _error_text(
            "invalid_args", {"fields": [".".join(str(p) for p in e["loc"]) for e in exc.errors()]}
        )
        call.risk = tool.risk
        return None
    call.args_json = args.model_dump(mode="json")
    call.risk = tool.effective_risk(args)
    call.target = proposals.target_of(tool, args)
    # caps (an MCP session's caps are per minute / day / hour -- ``placeholders.MCP_CAPS``, enforced by Epic 73)
    used_tool = await _tool_calls_of(db, run.id, tool.key) + counts.get(tool.key, 0) if external is None else 0
    if external is None and (
        run.tool_call_count + counts.get("_all", 0) >= limits.max_tool_calls
        or used_tool >= tool.max_calls_per_run
        or (call.risk != "read" and run.write_count >= limits.max_writes)
    ):
        call.outcome, call.error_code, call.mode_reason = "refused", "limit_reached", "limit_reached"
        call.result_text = _error_text("limit_reached")
        return None
    # Effective Rights re-checked before every tool call (a revoked permission / deactivated owner fails the next call)
    try:
        principal = await effective_principal(
            db, run, spec, oauth_scopes=external.oauth_scopes if external is not None else None
        )
    except OwnerUnavailable:
        call.outcome, call.error_code = "refused", "owner_unavailable"
        call.result_text = _error_text("owner_unavailable")
        return "owner_unavailable"
    ctx = dataclasses.replace(ctx, principal=principal)
    # subject binding
    if call.target is not None and run.subject_id is not None and not tool.scope.cross_subject:
        if str(call.target[1]) not in _related_ids(run):
            call.outcome, call.error_code = "not_found", "not_found"
            call.result_text = _error_text("not_found")
            return None
    send_intent = None
    grounded_ok = False
    if tool.egress and tool.send_intent is not None:
        tctx = state.ToolCtx(
            request=ctx, db=db, actor=ai_actor(run), run_id=run.id, agent_key=run.agent_key, owner_user_id=run.owner_user_id,  # type: ignore[arg-type]
            subject=(run.subject_type, run.subject_id) if run.subject_id else None,  # type: ignore[arg-type]
            idempotency_key=call.idempotency_key, execution="agent",
        )  # fmt: skip
        token = set_context(ctx)
        try:
            intent = await tool.send_intent(tctx, args)
        except NotFound:
            intent = None
        finally:
            reset_context(token)
        if intent is None:
            call.outcome, call.error_code = "not_found", "not_found"
            call.result_text = _error_text("not_found")
            return None
        channels = list(
            (config.channels if config is not None and config.channels else None) or spec.channels
        )
        decision = await send_policy.check(
            ctx, db, intent, args=call.raw, subject=(run.subject_type, run.subject_id) if run.subject_id else None,  # type: ignore[arg-type]
            related_refs=run.related_refs or [], candidate_ids=run.candidate_ids or [], allowed_channels=channels,
        )  # fmt: skip
        if not decision.ok:
            call.outcome, call.error_code, call.mode_reason = (
                "policy_violation",
                decision.violation,
                decision.violation or "send_policy",
            )
            call.result_text = _error_text(decision.violation or "send_policy")
            run.policy_violations += 1
            return _violation_end(run, external)
        send_intent, grounded_ok = decision.intent, decision.grounded_ok
    mode = resolve(
        _policy_input(
            run,
            spec,
            tool,
            args,
            gates,
            principal,
            allow,
            config,
            policies,
            send_intent=send_intent,
            grounded_ok=grounded_ok,
            external=external,
        )
    )
    call.mode, call.mode_reason, call.risk = mode.mode, mode.reason, mode.risk
    if mode.mode == "off":
        if external is not None and mode.reason == "policy_violation":
            call.outcome, call.error_code = "policy_violation", "policy_violation"
            call.result_text = _error_text("policy_violation")
            run.policy_violations += 1
            return _violation_end(run, external)
        call.outcome, call.error_code = "refused", mode.reason
        call.result_text = _error_text("refused", {"reason": mode.reason})
        return None
    if mode.mode == "suggest" and call.risk != "read":
        tctx = state.ToolCtx(
            request=ctx, db=db, actor=ai_actor(run), run_id=run.id, agent_key=run.agent_key, owner_user_id=run.owner_user_id,  # type: ignore[arg-type]
            subject=(run.subject_type, run.subject_id) if run.subject_id else None,  # type: ignore[arg-type]
            idempotency_key=call.idempotency_key, execution="agent",
        )  # fmt: skip
        evidence = [
            e
            for e in (reserved.get("_evidence") or [])
            if isinstance(e, dict) and str(e.get("id")) in _related_ids(run)
        ]
        confidence = reserved.get("_confidence")
        token = set_context(ctx)
        try:
            async with db.begin_nested():
                proposal = await proposals.create(
                    tctx, tool, args, run=run, origin=proposal_origin(run.origin),
                    rationale=str(reserved.get("_rationale") or "") or None,
                    confidence=float(confidence) if isinstance(confidence, int | float) else None,
                    evidence=evidence, risk=call.risk,
                    title_prefix=external.proposal_label if external is not None else "",
                    app_installation_id=external.app_installation_id if external is not None else None,
                )  # fmt: skip
        except DomainError as exc:
            call.outcome, call.error_code = "failed", exc.code[:50]
            call.result_text = _error_text(exc.code)
            return None
        finally:
            reset_context(token)
        call.outcome, call.proposal_id = "proposed", proposal.id
        call.idempotency_key = proposal.idempotency_key
        call.summary = f"proposal {proposal.human_id} created"
        call.result = {"proposal": proposal.human_id, "status": "pending_human_approval"}
        call.result_text = json.dumps(
            {
                "message": f"Proposal {proposal.human_id} is waiting for a person to approve it.",
                "proposal": proposal.human_id,
                "status": "pending_human_approval",
            }
        )
        return None
    # auto (or a read): idempotency, then execute
    call.idempotency_key = _idem_key(tool, run, step_no, args, call.target)
    earlier = (
        await db.execute(select(AgentToolCall).where(AgentToolCall.idempotency_key == call.idempotency_key))
    ).scalar_one_or_none()
    if earlier is not None:
        call.outcome = "deduplicated"
        call.summary = earlier.result_summary or "already done"
        call.idempotency_key = f"dedup:{run.id}:{call.tool_use_id}"
        call.result_text = json.dumps({"status": "already_done", "summary": call.summary})
        return None
    await _execute_tool(db, run, call, args, ctx, step_no)
    if call.outcome != "executed":
        call.idempotency_key = f"call:{run.id}:{call.tool_use_id}"
        call.result_text = _error_text(call.error_code or call.outcome)
    return None


def _violation_end(run: AgentRun, external: ExternalCall | None) -> str | None:
    """End a tainted run on a policy violation at once; an MCP session (tainted by definition) only at the limit."""
    if external is not None:
        return "policy_violation" if run.policy_violations >= POLICY_VIOLATION_LIMIT else None
    return "policy_violation" if run.tainted or run.policy_violations >= POLICY_VIOLATION_LIMIT else None


async def log_call(
    db: AsyncSession, run: AgentRun, call: _Call, *, step_no: int, step_id: uuid.UUID | None, index: int
) -> AgentToolCall:
    """One ``agent_tool_calls`` row (+ the encrypted args / result payloads) for a processed tool use."""
    row = AgentToolCall(
        id=uuid.uuid4(), tenant_id=run.tenant_id, run_id=run.id, step_id=step_id, step_no=step_no,
        tool_use_id=call.tool_use_id[:64], tool_key=(call.tool.key if call.tool else call.name)[:80],
        tool_version=call.tool.version if call.tool else 0, risk=call.risk if call.risk in ("read", "low", "high") else "high",
        mode=call.mode, mode_reason=call.mode_reason[:40], outcome=call.outcome,
        args_redacted=redact_for_log(call.args_json if call.args_json is not None else call.raw),
        result_summary=redact_for_log(call.summary)[:500] if call.summary else None, error_code=(call.error_code or None) and call.error_code[:50],
        proposal_id=call.proposal_id, idempotency_key=call.idempotency_key[:200],
        target_type=call.target[0][:40] if call.target else None, target_id=call.target[1] if call.target else None,
        egress=bool(call.tool and call.tool.egress), candidate_ids=list(call.candidate_ids),
        latency_ms=call.latency_ms, executed_as=call.executed_as,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    payloads.store(
        db,
        run,
        "tool_args",
        call.raw,
        step_no=step_no,
        seq=payloads.SEQ_TOOL + 2 * index,
        step_id=step_id,
        tool_call_id=row.id,
    )
    if call.result is not None:
        payloads.store(
            db,
            run,
            "tool_result",
            call.result,
            step_no=step_no,
            seq=payloads.SEQ_TOOL + 2 * index + 1,
            step_id=step_id,
            tool_call_id=row.id,
        )
    return row


# ================================================================ tx B
async def _apply(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    step_no: int,
    worker_id: str,
    claim: _Claim,
    result: StepResult,
    *,
    retryable_attempt: int,
) -> StepOutcome:
    spec = claim.spec
    async with tenant_session(tenant_id) as db:
        run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())).scalar_one()
        if run.status != "running" or run.lease_owner != worker_id or run.next_step_no != step_no:
            await db.execute(
                pg_insert(AgentRunStep)
                .values(
                    id=uuid.uuid4(), tenant_id=tenant_id, run_id=run_id, step_no=step_no, ai_action_id=result.action_id,
                    stop_reason=result.stop_reason, status="dropped", error_code=result.error_code,
                    input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "run_id", "step_no"])
            )  # fmt: skip
            return StepOutcome(note="dropped")
        code = result.error_code
        if not result.ok and code in ("timeout", "provider_error") and retryable_attempt < len(RETRY_BACKOFF):
            run.retry_count += 1
            run.lease_owner, run.lease_expires_at = None, None
            return StepOutcome(
                next_step=step_no, countdown=RETRY_BACKOFF[retryable_attempt], note="provider_retry"
            )
        step = AgentRunStep(
            id=uuid.uuid4(), tenant_id=tenant_id, run_id=run.id, step_no=step_no, ai_action_id=result.action_id,
            stop_reason=result.stop_reason, status="ok" if result.ok else "failed", error_code=code,
            input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens, cache_write_tokens=result.usage.cache_write_tokens,
            request_sha256=payloads.sha256_of(list(claim.request.messages)),
            response_sha256=payloads.sha256_of(list(result.content)) if result.content else None,
        )  # fmt: skip
        db.add(step)
        await db.flush()
        run.step_count += 1
        run.input_tokens += result.usage.input_tokens
        run.output_tokens += result.usage.output_tokens
        from app.services.ai.pricing import cost_micros

        run.cost_micros += cost_micros(result.model or "", result.usage)
        if claim.warning:
            payloads.store(
                db,
                run,
                "request_delta",
                claim.warning,
                step_no=step_no,
                seq=payloads.SEQ_WARNING,
                step_id=step.id,
            )
        if not result.ok:
            mapping = {
                "timeout": ("failed", "provider_error"), "provider_error": ("failed", "provider_error"),
                "refused": ("failed", "refused"), "consent_missing": ("failed", "consent_missing"),
                "kill_switch": ("stopped", "kill_switch"), "global_kill": ("stopped", "kill_switch"),
                "feature_disabled": ("cancelled", "manual"), "budget_exceeded": ("stopped", "budget"),
                "agent_budget": ("stopped", "budget"), "not_configured": ("failed", "provider_error"),
                "truncated": ("failed", "provider_error"),
            }  # fmt: skip
            status, reason = mapping.get(code or "", ("failed", "provider_error"))
            await finalize(db, run, status, reason, spec=spec)
            return StepOutcome(status=status)
        payloads.store(
            db,
            run,
            "response",
            list(result.content),
            step_no=step_no,
            seq=payloads.SEQ_RESPONSE,
            step_id=step.id,
        )
        if result.stop_reason == "pause_turn":
            run.next_step_no = step_no + 1
            run.lease_owner, run.lease_expires_at = None, None
            return StepOutcome(next_step=step_no + 1)
        if not result.tool_uses:
            summary = (result.text or "").strip()[:2000]
            await finalize(
                db, run, "completed", None, spec=spec, result={"outcome": "done", "summary": summary}
            )
            return StepOutcome(status="completed")
        limits = _limits(run)
        gates = await state.gates(db)
        config = await state.config_for(db, spec.key)
        policies = await state.action_policies(db, spec.key)
        allow = state.allow_list(spec, gates.features, claim.ctx.principal)
        blocks: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        end: tuple[str, str | None, dict[str, Any] | None, bool] | None = (
            None  # status, reason, result, fallback
        )
        logged: list[_Call] = []
        for index, use in enumerate(result.tool_uses):
            call = _Call(tool_use_id=use.id, name=use.name, raw=dict(use.input))
            if end is not None or index >= limits.max_parallel_tool_calls:
                code_ = "run_ended" if end is not None else "too_many_parallel_calls"
                blocks.append(tool_result_block(use.id, _error_text(code_), is_error=True))
                continue
            if use.name == "emit_result":
                model = spec.result_model or EmitResult
                try:
                    data = model.model_validate(use.input).model_dump(mode="json")
                except ValidationError as exc:
                    blocks.append(
                        tool_result_block(
                            use.id,
                            _error_text(
                                "invalid_result",
                                {"fields": [".".join(str(p) for p in e["loc"]) for e in exc.errors()]},
                            ),
                            is_error=True,
                        )
                    )
                    continue
                needs_human = data.get("outcome") == "needs_human"
                end = ("completed", "manual" if needs_human else None, data, needs_human)
                blocks.append(tool_result_block(use.id, '{"status": "completed"}'))
                continue
            if use.name == "await_event" and limits.max_wait_hours > 0:
                try:
                    wait = AwaitEventIn.model_validate(use.input)
                except ValidationError:
                    blocks.append(tool_result_block(use.id, _error_text("invalid_args"), is_error=True))
                    continue
                if not WAIT_CONDITION_RE.fullmatch(wait.condition) or wait.hours > limits.max_wait_hours:
                    blocks.append(tool_result_block(use.id, _error_text("invalid_wait"), is_error=True))
                    continue
                run.status = "waiting"
                run.wait_condition, run.wait_ref = wait.condition, wait.ref
                run.wait_until = _now() + timedelta(hours=wait.hours)
                blocks.append(tool_result_block(use.id, '{"status": "waiting"}'))
                end = ("waiting", None, None, False)
                continue
            reason = await _process_call(  # type: ignore[assignment]
                db, run, spec, call, step_no=step_no, gates=gates, allow=allow, config=config, policies=policies,
                counts=counts, ctx=claim.ctx,
            )  # fmt: skip
            counts["_all"] = counts.get("_all", 0) + 1
            if call.tool is not None:
                counts[call.tool.key] = counts.get(call.tool.key, 0) + 1
            logged.append(call)
            ok = call.outcome in ("executed", "proposed", "deduplicated")
            blocks.append(
                tool_result_block(use.id, call.result_text or _error_text(call.outcome), is_error=not ok)
            )
            if reason is not None:
                end = ("failed", reason, None, True)
        for i, call in enumerate(logged):
            await log_call(db, run, call, step_no=step_no, step_id=step.id, index=i)
        run.tool_call_count += len(logged)
        delta = [{"role": "user", "content": blocks}]
        payloads.store(
            db, run, "request_delta", delta, step_no=step_no + 1, seq=payloads.SEQ_DELTA, step_id=step.id
        )
        run.next_step_no = step_no + 1
        run.lease_owner, run.lease_expires_at = None, None
        if end is not None:
            status, reason, data, wants_fallback = end  # type: ignore[assignment]
            if status == "waiting":
                await db.flush()
                await locks.release(run.tenant_id, run.agent_key, _subject_key(run), run.id)
                return StepOutcome(status="waiting")
            if status == "completed":
                await finalize(
                    db, run, "completed", reason, spec=spec, result=data, run_fallback=wants_fallback
                )
                return StepOutcome(status="completed")
            await finalize(db, run, status, reason, spec=spec, run_fallback=wants_fallback)
            return StepOutcome(status=status)
        return StepOutcome(next_step=step_no + 1)


async def run_step(
    tenant_id: uuid.UUID, run_id: uuid.UUID, step_no: int, *, worker_id: str, attempt: int = 0
) -> StepOutcome:
    """One step of a run (see module docstring). ``attempt`` counts provider-error retries of this step."""
    claim = await _claim(tenant_id, run_id, step_no, worker_id, 16_000)
    if isinstance(claim, StepOutcome):
        return claim
    sink = _stream_sink(run_id) if claim.spec.origin in ("copilot", "portal") else None  # Epic 77: portal Copilots stream too
    result = await _call_model(tenant_id, claim, sink)
    if not result.ok and result.error_code == "truncated":
        retry = claim.request.model_copy(update={"max_tokens": claim.request.max_tokens * 2})
        second = await _call_model(tenant_id, dataclasses.replace(claim, request=retry), sink)
        usage = second.usage.model_copy(
            update={
                "input_tokens": second.usage.input_tokens + result.usage.input_tokens,
                "output_tokens": second.usage.output_tokens + result.usage.output_tokens,
            }
        )
        result = second.model_copy(update={"usage": usage})
    return await _apply(tenant_id, run_id, step_no, worker_id, claim, result, retryable_attempt=attempt)


StepSink = Any
STREAM_MAXLEN = 2000


def _stream_sink(run_id: uuid.UUID) -> StepSink:
    """Publish Copilot text deltas to the Redis Stream ``agents:stream:<run_id>`` (Epic 54 serves it as SSE)."""

    async def sink(kind: str, data: Mapping[str, Any]) -> None:
        try:
            from redis.asyncio import Redis

            from app.core.settings import get_settings

            s = get_settings()
            client = Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=0,
                password=s.redis_password or None,
                socket_timeout=0.5,
            )
            try:
                key = f"agents:stream:{run_id}"
                await client.xadd(key, {"event": kind, "data": json.dumps(dict(data))}, maxlen=STREAM_MAXLEN)
                await client.expire(key, 900)
            finally:
                await client.aclose()
        except Exception:  # streaming is best-effort; the final content is stored by tx B
            logger.debug("Copilot stream publish failed for run {}", run_id)

    return sink


async def _call_model(tenant_id: uuid.UUID, claim: _Claim, sink: StepSink) -> StepResult:
    token = set_context(claim.ctx)
    try:
        async with tenant_session(tenant_id) as db:
            if sink is None:
                return await ai.run_agent_step(claim.ctx, db, claim.request)
            return await ai.stream_agent_step(claim.ctx, db, claim.request, sink)
    finally:
        reset_context(token)


__all__ = [
    "ExternalCall",
    "StepOutcome",
    "finalize",
    "is_portal_owned",
    "log_call",
    "proposal_origin",
    "queue_for_origin",
    "run_step",
    "tool_defs",
]
