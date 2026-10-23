# ruff: file-ignore[line-too-long]
"""External (MCP) sessions on the agent runtime (Epic 66, spec §0.15, AD-84). Epic 73's MCP server drives them.

* ``open_external_session`` -- one ``agent_runs`` row per MCP session: agent ``mcp``, origin / trigger ``external``,
  status ``running``, owner = the delegated token's user, ``tainted`` always, ``model_tier`` ``external``. It holds no
  lease (the step reaper never touches it); ``close_idle_external`` closes sessions idle > 30 minutes.
* ``execute_external_call`` -- one tool call through the SAME executor path as a model tool call
  (``executor._process_call``): schema validation, Effective Rights (delegated user rights ∩ the ``mcp`` permission
  ceiling, recomputed from the live user), subject / ref binding, ``policy.resolve`` (origin ``external``), the send
  policy, Proposals for ``suggest`` (labelled "Requested by <client> for <User> through MCP"; never executed), one
  ``agent_tool_calls`` row, field masking, untrusted wrapping and a 32 KB result cap. ``call_id`` (the JSON-RPC id)
  deduplicates within the session.
* ``close_external_session`` -- ``completed`` or ``stopped`` (``kill_switch`` | ``manual`` | ``policy_violation`` | ...).
"""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.agentic.agents import AgentSpec, get_agent
from app.core.agentic.exposure import get_exposure
from app.core.agentic.tools import tools
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.events.agents import AgentRunStarted
from app.models.agent_runtime import (
    ACTIVE_RUN_STATUSES,
    FINAL_RUN_STATUSES,
    REASON_CODES,
    AgentProposal,
    AgentRun,
    AgentToolCall,
)
from app.models.ecosystem import EcosystemSettings
from app.schemas.agentic import ExternalCallIn, ExternalCallOut, ExternalSessionIn
from app.services import directory_service, entity_refs
from app.services.agent_runtime import state
from app.services.agent_runtime.executor import ExternalCall, _Call, _process_call, finalize, log_call
from app.services.agent_runtime.principals import (
    OwnerUnavailable,
    ensure_agent_principal,
    owner_principal,
    tool_context_request,
)
from app.services.ai.loop import wrap_tool_result

MCP_AGENT_KEY = "mcp"
SESSION_MAX_HOURS = 24
IDLE_MINUTES = 30
RESULT_CAP_BYTES = 32 * 1024
CLIENT_NAME_MAX = 120
CLOSE_REASONS = frozenset(
    {"completed", "kill_switch", "manual", "policy_violation", "owner_unavailable", "timeout"}
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _now() -> datetime:
    return datetime.now(UTC)


def sanitize_client_name(name: str) -> str:
    """MCP ``clientInfo.name``: control characters removed, whitespace collapsed, at most 120 characters."""
    cleaned = " ".join(_CONTROL.sub(" ", name or "").split())
    return (cleaned or "MCP client")[:CLIENT_NAME_MAX]


def _app_principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if (
        not isinstance(principal, Principal)
        or principal.type != "app"
        or principal.acting_user_id is None
        or principal.app_installation_id is None
    ):
        raise Forbidden("A delegated MCP token is required", code="app_token_required")
    return principal


def _spec() -> AgentSpec:
    try:
        return get_agent(MCP_AGENT_KEY)
    except KeyError as exc:  # pragma: no cover - the placeholder is registered by agent_runtime.hooks
        raise NotFound("MCP is not available") from exc


async def mcp_groups_enabled(db: AsyncSession) -> frozenset[str]:
    """Return the tenant's enabled MCP tool groups (``ecosystem_settings``; empty until MCP is enabled)."""
    row = (await db.execute(select(EcosystemSettings))).scalar_one_or_none()
    if row is None or row.mcp_enabled_at is None:
        return frozenset()
    return frozenset(row.mcp_tool_groups or ())


# ================================================================ open
async def open_external_session(ctx: RequestContext, db: AsyncSession, data: ExternalSessionIn) -> uuid.UUID:
    principal = _app_principal(ctx)
    if principal.acting_user_id != data.user_id or principal.app_installation_id != data.installation_id:
        raise Forbidden("The token does not match the session", code="app_token_mismatch")
    spec = _spec()
    tenant_id = ctx.tenant_id or entity_refs.session_tenant(db)
    gates = await state.gates(db)
    if gates.any_kill:
        raise Forbidden("AI agents are stopped", code="kill_switch")
    if not gates.agentic_on or not gates.agent_on(MCP_AGENT_KEY):
        raise Forbidden("MCP access is turned off", code="feature_off")
    try:
        await owner_principal(db, tenant_id, data.user_id)
    except OwnerUnavailable as exc:
        raise Forbidden("The user is not active", code="owner_unavailable") from exc
    dedup = f"mcp:{data.installation_id}:{data.mcp_session_id}"[:200] if data.mcp_session_id else None
    if dedup is not None:
        existing = await _active_by_dedup(db, dedup)
        if existing is not None:
            if existing.owner_user_id != data.user_id:
                raise Forbidden("The token does not match the session", code="app_token_mismatch")
            return existing.id
    principal_row = await ensure_agent_principal(db, tenant_id, spec)
    starter = actor_from_ctx(ctx)
    now = _now()
    run = AgentRun(
        id=uuid.uuid4(), tenant_id=tenant_id, agent_key=MCP_AGENT_KEY, agent_principal_id=principal_row.id,
        spec_version=spec.version, origin="external", trigger_kind="external", trigger_key=None, dedup_key=dedup,
        owner_user_id=data.user_id, owner_principal_type="user", owner_principal_id=data.user_id,
        app_installation_id=data.installation_id, external_client=sanitize_client_name(data.client_name),
        started_by_kind=starter.kind, started_by_id=starter.id, candidate_ids=[], related_refs=[], status="running",
        model_tier="external", prompt_ref=spec.prompt_id[:120], limits=spec.limits.to_json(), next_step_no=1,
        step_count=0, tool_call_count=0, write_count=0, retry_count=0, input_tokens=0, output_tokens=0, cost_micros=0,
        tainted=True, policy_violations=0, started_at=now, deadline_at=now + timedelta(hours=SESSION_MAX_HOURS),
    )  # fmt: skip
    try:
        async with db.begin_nested():
            db.add(run)
            await db.flush()
    except IntegrityError:
        existing = await _active_by_dedup(db, dedup) if dedup else None
        if existing is None:
            raise
        return existing.id
    emit(db, AgentRunStarted(
        tenant_id=tenant_id, run_id=run.id, agent_key=MCP_AGENT_KEY, origin="external", trigger_kind="external",
        subject_type=None, subject_id=None, owner_user_id=data.user_id,
    ))  # fmt: skip
    audit.record(
        "agent_run.started", "agent_run", run.id, session=db, agent_key=MCP_AGENT_KEY, trigger_kind="external",
        app_installation_id=str(data.installation_id), token_id=str(data.token_id),
    )  # fmt: skip
    await db.flush()
    return run.id


async def _active_by_dedup(db: AsyncSession, dedup: str) -> AgentRun | None:
    return (
        await db.execute(
            select(AgentRun).where(
                AgentRun.agent_key == MCP_AGENT_KEY,
                AgentRun.dedup_key == dedup,
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def _session(db: AsyncSession, principal: Principal, run_id: uuid.UUID) -> AgentRun:
    run = (
        await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if (
        run is None
        or run.agent_key != MCP_AGENT_KEY
        or run.origin != "external"
        or run.owner_user_id != principal.acting_user_id
        or run.app_installation_id != principal.app_installation_id
    ):
        raise NotFound("Session not found")
    return run


# ================================================================ call
def proposal_url(proposal_id: uuid.UUID) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/ai/proposals/{proposal_id}"


def _mask(data: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """§0.5 field masking by the token's ``pii.*`` scopes (``core.public_api``, when shipped) on top of the tool's
    own masking for the user."""
    try:
        from app.core.public_api import mask_for_scopes
    except ImportError:  # pragma: no cover - the Public API conventions module ships with Epic 66.3
        return data
    masked = mask_for_scopes(data, principal)
    return dict(masked) if isinstance(masked, dict) else data


def _cap(data: dict[str, Any], max_results: int) -> tuple[dict[str, Any], bool]:
    """FR-379: lists trimmed to ``max_results``; the whole result to 32 KB (largest list halved until it fits)."""
    truncated = False

    def trim(node: Any) -> Any:
        nonlocal truncated
        if isinstance(node, dict):
            return {k: trim(v) for k, v in node.items()}
        if isinstance(node, list):
            if len(node) > max_results:
                truncated = True
                node = node[:max_results]
            return [trim(v) for v in node]
        return node

    out = trim(data)

    def size(obj: Any) -> int:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def largest_list(node: Any, best: list[Any] | None = None) -> list[Any] | None:
        if isinstance(node, dict):
            for v in node.values():
                best = largest_list(v, best)
        elif isinstance(node, list):
            if len(node) > 1 and (best is None or len(node) > len(best)):
                best = node
            for v in node:
                best = largest_list(v, best)
        return best

    while size(out) > RESULT_CAP_BYTES:
        target = largest_list(out)
        if target is None:
            text = json.dumps(out, ensure_ascii=False, default=str)
            return {"text": text.encode("utf-8")[: RESULT_CAP_BYTES - 1024].decode("utf-8", "ignore")}, True
        del target[len(target) // 2 :]
        truncated = True
    return out, truncated


async def _label(db: AsyncSession, run: AgentRun) -> str:
    names = await directory_service.user_names(db, [run.owner_user_id])  # type: ignore[list-item]
    user = names.get(run.owner_user_id) or "a user"  # type: ignore[arg-type]
    return f"Requested by {run.external_client or 'an MCP client'} for {user} through MCP"


async def _dedup(db: AsyncSession, run: AgentRun, call_id: str) -> ExternalCallOut | None:
    earlier = (
        await db.execute(
            select(AgentToolCall).where(AgentToolCall.run_id == run.id, AgentToolCall.tool_use_id == call_id)
        )
    ).scalar_one_or_none()
    if earlier is None:
        return None
    return ExternalCallOut(
        outcome="deduplicated", result={"status": "already_handled", "outcome": earlier.outcome},
        proposal_id=earlier.proposal_id, proposal_url=proposal_url(earlier.proposal_id) if earlier.proposal_id else None,
        error_code=earlier.error_code,
    )  # fmt: skip


async def execute_external_call(
    ctx: RequestContext, db: AsyncSession, data: ExternalCallIn
) -> ExternalCallOut:
    principal = _app_principal(ctx)
    run = await _session(db, principal, data.run_id)
    if run.status != "running":
        return ExternalCallOut(outcome="refused", error_code="session_closed")
    call_id = data.call_id[:64]
    duplicate = await _dedup(db, run, call_id)
    if duplicate is not None:
        return duplicate
    spec = _spec()
    gates = await state.gates(db)
    if gates.any_kill:
        await close_external_session(ctx, db, run.id, reason="kill_switch")
        return ExternalCallOut(outcome="refused", error_code="kill_switch")
    tool = tools().get(data.tool_key)
    step_no = run.next_step_no
    run.next_step_no = step_no + 1
    external = ExternalCall(
        oauth_scopes=frozenset(principal.oauth_scopes), groups_enabled=await mcp_groups_enabled(db),
        proposal_label=await _label(db, run), app_installation_id=run.app_installation_id,
    )  # fmt: skip
    call = _Call(
        tool_use_id=call_id,
        name=tool.wire_name if tool is not None else data.tool_key.replace(".", "__", 1)[:80],
        raw=dict(data.args),
    )
    base = Principal(type="agent", id=run.agent_principal_id, tenant_id=run.tenant_id)
    end = await _process_call(
        db, run, spec, call, step_no=step_no, gates=gates, allow=state.allow_list(spec, gates.features, None),
        config=await state.config_for(db, MCP_AGENT_KEY), policies=await state.action_policies(db, MCP_AGENT_KEY),
        counts={}, ctx=tool_context_request(run, base, step_no), external=external,
    )  # fmt: skip
    await log_call(db, run, call, step_no=step_no, step_id=None, index=0)
    run.tool_call_count += 1
    out = _out(call, principal, run)
    if end is not None:
        await finalize(db, run, "stopped", end, spec=spec, run_fallback=False)
    await db.flush()
    return out


def _out(call: _Call, principal: Principal, run: AgentRun) -> ExternalCallOut:  # ruff: ignore[unused-function-argument]
    outcome = call.outcome
    if outcome == "executed":
        tool = call.tool
        assert tool is not None
        exposure = get_exposure(tool.key)
        masked = _mask(dict(call.result or {}), principal)
        wrapped = json.loads(wrap_tool_result(masked, tool.untrusted_output, source=tool.key))
        result, truncated = _cap(wrapped, exposure.max_results if exposure is not None else 50)
        return ExternalCallOut(
            outcome="executed", result=result, truncated=truncated, candidate_ids=list(call.candidate_ids)
        )
    if outcome == "proposed":
        assert call.proposal_id is not None
        return ExternalCallOut(
            outcome="proposed",
            result={
                "status": "pending_human_approval",
                "proposal": (call.result or {}).get("proposal"),
                "message": "Not executed: a person in Talent OS must approve this request.",
            },
            proposal_id=call.proposal_id,
            proposal_url=proposal_url(call.proposal_id),
        )
    if outcome == "deduplicated":
        return ExternalCallOut(
            outcome="deduplicated", result={"status": "already_done", "summary": call.summary}
        )
    if outcome == "policy_violation":
        return ExternalCallOut(outcome="refused", error_code=call.error_code or "policy_violation")
    mapped = outcome if outcome in ("refused", "invalid", "not_found", "failed") else "failed"
    detail: dict[str, Any] | None = None
    if call.result_text:
        try:
            parsed = json.loads(call.result_text)
            detail = parsed if isinstance(parsed, dict) else None
        except ValueError:
            detail = None
    return ExternalCallOut(outcome=mapped, result=detail, error_code=call.error_code or mapped)


# ================================================================ close
async def close_external_session(
    ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, *, reason: str
) -> None:
    """``reason`` ``completed`` -> status completed; otherwise stopped with that reason code. Allowed for the session's
    own token or a system context (kill switch, uninstall, revocation handlers). Idempotent."""
    if reason not in CLOSE_REASONS or (reason != "completed" and reason not in REASON_CODES):
        raise ValidationFailed("Unknown close reason", code="invalid_reason_code")
    if ctx.principal is None:
        run = (
            await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None or run.origin != "external":
            raise NotFound("Session not found")
    else:
        run = await _session(db, _app_principal(ctx), run_id)
    if run.status in FINAL_RUN_STATUSES:
        return
    if reason == "completed":
        await finalize(db, run, "completed", None, run_fallback=False)
    else:
        await finalize(db, run, "stopped", reason, run_fallback=False)


async def close_idle_external(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Beat ``agents.close_idle_external``: stop ``external`` sessions idle > 30 minutes (or past their deadline)."""
    now = now or _now()
    rows = list(
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.origin == "external",
                    AgentRun.status == "running",
                    (AgentRun.updated_at < now - timedelta(minutes=IDLE_MINUTES))
                    | (AgentRun.deadline_at < now),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for run in rows:
        await finalize(db, run, "stopped", "timeout", run_fallback=False)
    await db.flush()
    return len(rows)


async def proposals_of(db: AsyncSession, run_id: uuid.UUID) -> list[AgentProposal]:
    """Return the Proposals an MCP session created (tests / Epic 73)."""
    return list((await db.execute(select(AgentProposal).where(AgentProposal.run_id == run_id))).scalars())


__all__ = [
    "CLOSE_REASONS",
    "IDLE_MINUTES",
    "MCP_AGENT_KEY",
    "RESULT_CAP_BYTES",
    "close_external_session",
    "close_idle_external",
    "execute_external_call",
    "mcp_groups_enabled",
    "open_external_session",
    "proposal_url",
    "proposals_of",
    "sanitize_client_name",
]
