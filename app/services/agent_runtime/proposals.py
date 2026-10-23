# ruff: file-ignore[line-too-long]
"""Proposals (Epic 53, story 53.5, AD-62 as reconciled: no approvals engine; FR-268/FR-269/FR-325).

* ``create`` -- a suggest-mode write (agent) or a Copilot Draft Action: preview, target + version, routing (action policy
  ``route_role_key`` else the Accountable Owner), expiry, idempotent per (tool, target, args, run).
* ``decide`` (★, human only) -- reject (note required) or approve / edit-and-approve: re-validate edited args (risk may
  not rise, tool unchanged), staleness (target version changed -> 409 ``proposal_stale``), the approver's live
  permissions, then execute the tool AS THE APPROVER in a SAVEPOINT (audit ``via_proposal_id``). A handler error ->
  ``execution_failed``; the decision stands. Every ``ai_actions`` row of the run gets the decision written back.
* ``bulk_decide`` -- same agent, tool and Demand only. ``confirm_draft`` / ``discard_draft`` -- Copilot owner only.
* ``expire_due`` / ``expiring_soon`` -- beat ``agents.expire_proposals``.

Epic 66 (§0.15): Proposals of ``external`` (MCP) runs carry ``app_installation_id`` and a title prefixed "Requested by
<client> for <User> through MCP"; Drafts of portal-owned runs copy the run's owner principal (no internal user owner;
routed to the ``PORTAL_OWNER_ROUTE`` sentinel so the ``routed`` CHECK holds) and are confirmed / discarded by that portal
principal only.
"""

import asyncio
import hashlib
import inspect
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef, actor_from_ctx, require_human
from app.core.agentic.agents import get_agent
from app.core.agentic.tools import get_tool
from app.core.agentic.types import RISK_ORDER, ProposalPreview, ToolSpec
from app.core.context import RequestContext, reset_context, set_context
from app.core.errors import ConflictError, DomainError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal, Scope
from app.core.sequences import next_human_id
from app.core.settings import get_settings
from app.events.agents import ProposalCreated, ProposalDecided
from app.models.agent_runtime import AgentProposal, AgentRun
from app.models.ai import AIAction
from app.schemas.agentic import BulkDecisionIn, ProposalDecisionIn, ProposalOut, ProposalQuery
from app.services import directory_service
from app.services.agent_runtime import state
from app.services.agent_runtime.principals import owner_principal

# Epic 66: Drafts of portal-owned runs have no internal assignee; this route key is never a real role.
PORTAL_OWNER_ROUTE = "portal_owner"
PORTAL_PRINCIPAL_TYPES = ("client_user", "vendor_user", "candidate")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def target_of(spec: ToolSpec, args: BaseModel) -> tuple[str, uuid.UUID] | None:
    if spec.scope.target_arg is None:
        return None
    raw = getattr(args, spec.scope.target_arg, None)
    if raw is None:
        return None
    try:
        target_id = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except ValueError:
        return None
    resource = spec.scope.resource
    if resource in ("entity", "record"):
        resource = str(
            getattr(args, "entity_type", None) or getattr(args, "recipient_kind", None) or resource
        )
    return resource, target_id


class VersionProbe:
    """Argument of ``ScopeRule.version`` (deviation recorded: the runtime has no generic target loader, so the version
    callable gets the tool context, the args and the target and may return a string or an awaitable string)."""

    def __init__(self, tctx: Any, args: BaseModel, target: tuple[str, uuid.UUID] | None) -> None:
        self.tctx, self.db, self.args, self.target = tctx, tctx.db, args, target


async def version_of(spec: ToolSpec, tctx: Any, args: BaseModel) -> str | None:
    if spec.scope.version is None:
        return None
    value = spec.scope.version(VersionProbe(tctx, args, target_of(spec, args)))
    if inspect.isawaitable(value):
        value = await value
    return None if value is None else str(value)[:100]


def _candidate_ids(spec: ToolSpec, args: BaseModel, run: AgentRun | None) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = list(run.candidate_ids or []) if run is not None else []
    if spec.scope.candidate_arg:
        raw = getattr(args, spec.scope.candidate_arg, None)
        if raw is not None:
            try:
                cid = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
                if cid not in ids:
                    ids.append(cid)
            except ValueError:
                pass
    return ids


async def create(
    tctx: Any,
    spec: ToolSpec,
    args: BaseModel,
    *,
    run: AgentRun | None,
    origin: str,
    rationale: str | None = None,
    confidence: float | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    risk: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    title_prefix: str = "",
    app_installation_id: uuid.UUID | None = None,
) -> AgentProposal:
    db: AsyncSession = tctx.db
    assert spec.preview is not None  # register_tool guarantees it for every non-read tool
    args_json = args.model_dump(mode="json")
    target = target_of(spec, args)
    run_ref = str(run.id) if run is not None else str(tctx.idempotency_key)
    key = f"proposal:{spec.key}:{target[1] if target else '-'}:{sha(args_json)[:16]}:{run_ref}"[:200]
    existing = (
        await db.execute(select(AgentProposal).where(AgentProposal.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    preview: ProposalPreview = await spec.preview(tctx, args)
    owner = owner_user_id or tctx.owner_user_id
    agent_key = run.agent_key if run is not None else tctx.agent_key
    policy = (await state.action_policies(db, agent_key)).get(spec.key)
    config = await state.config_for(db, agent_key)
    route_role = policy.route_role_key if policy is not None and origin == "agent" else None
    ttl = (
        config.proposal_expiry_hours if config is not None else None
    ) or get_settings().agent_proposal_default_ttl_hours
    effective_risk = risk or spec.effective_risk(args)
    portal_owned = run is not None and run.owner_principal_type not in (None, "user")
    if portal_owned:
        assert run is not None
        owner, route_role = None, PORTAL_OWNER_ROUTE
    title = f"{title_prefix}: {preview.title}" if title_prefix else preview.title
    proposal = AgentProposal(
        id=uuid.uuid4(),
        tenant_id=tctx.request.tenant_id,
        human_id=await next_human_id(db, "agent_proposal", "PRP"),
        origin=origin,
        run_id=run.id if run is not None else None,
        conversation_id=conversation_id or (run.conversation_id if run is not None else None),
        agent_key=agent_key,
        tool_key=spec.key,
        tool_version=spec.version,
        title=title[:200],
        args=args_json,
        args_sha256=sha(args_json),
        preview=preview.to_json(),
        target_type=target[0] if target else None,
        target_id=target[1] if target else None,
        target_version=await version_of(spec, tctx, args),
        rationale=(rationale or None) and rationale[:2000],
        confidence=Decimal(str(round(max(0.0, min(1.0, confidence)), 2))) if confidence is not None else None,
        evidence=[dict(e) for e in evidence][:10],
        risk="high" if effective_risk == "high" else "low",
        candidate_ids=_candidate_ids(spec, args, run),
        status="pending",
        assignee_user_id=None if route_role else owner,
        route_role_key=route_role,
        owner_user_id=owner,
        expires_at=datetime.now(UTC) + timedelta(hours=int(ttl)),
        idempotency_key=key,
    )
    if portal_owned:
        assert run is not None
        proposal.owner_principal_type = run.owner_principal_type
        proposal.owner_principal_id = run.owner_principal_id
        proposal.owner_org_kind = run.owner_org_kind
        proposal.owner_org_id = run.owner_org_id
    elif owner is not None:
        proposal.owner_principal_type, proposal.owner_principal_id = "user", owner
    proposal.app_installation_id = app_installation_id or (run.app_installation_id if run is not None else None)
    db.add(proposal)
    await db.flush()
    emit(
        db,
        ProposalCreated(
            tenant_id=proposal.tenant_id, proposal_id=proposal.id, run_id=proposal.run_id, agent_key=agent_key,
            tool_key=spec.key, origin=origin, target_type=proposal.target_type, target_id=proposal.target_id,
            assignee_user_id=proposal.assignee_user_id, route_role_key=proposal.route_role_key, expires_at=proposal.expires_at,
        ),
    )  # fmt: skip
    return proposal


async def create_draft_for(
    tctx: Any, tool_key: str, args: Mapping[str, Any], *, rationale: str | None = None
) -> AgentProposal:
    """For drafting tools (``messaging.create_draft``): a Proposal for ``tool_key`` (e.g. ``messaging.send``) in the
    calling run. The drafted tool is validated with its own input model."""
    spec = get_tool(tool_key)
    parsed = spec.input_model.model_validate(dict(args))
    run = await tctx.db.get(AgentRun, tctx.run_id) if tctx.run_id is not None else None
    origin = run.origin if run is not None and run.origin in ("copilot", "external", "portal") else "agent"
    return await create(
        tctx, spec, parsed, run=run, origin=origin, rationale=rationale, risk=spec.effective_risk(parsed)
    )


# ================================================================ reads
def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "api_key", "app"):  # Epic 66: +app
        raise NotFound("Resource not found")
    return principal


async def _role_member(db: AsyncSession, principal: Principal, role_key: str | None) -> bool:
    if not role_key:
        return False
    role_id = await directory_service.role_id_by_key(db, role_key)
    return role_id is not None and role_id in principal.role_ids


def _governor(principal: Principal) -> bool:
    return principal.has("ai:govern", Scope.TENANT) and principal.has("proposal:decide", Scope.TENANT)


async def can_decide(db: AsyncSession, principal: Principal, p: AgentProposal) -> bool:
    if p.status != "pending" or principal.type != "user":
        return False
    if p.origin == "copilot":
        return p.owner_user_id == principal.id
    if not principal.has("proposal:decide"):
        return False
    return (
        p.assignee_user_id == principal.id
        or await _role_member(db, principal, p.route_role_key)
        or _governor(principal)
    )


async def _visible(db: AsyncSession, principal: Principal, p: AgentProposal) -> bool:
    if p.origin == "copilot":
        return p.owner_user_id == principal.id  # Copilot drafts are private to their user
    if p.owner_user_id == principal.id or p.assignee_user_id == principal.id or principal.has("ai:govern"):
        return True
    return await _role_member(db, principal, p.route_role_key)


def _schema(spec: ToolSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    from app.services.ai.loop import output_schema

    return output_schema(spec.input_model)


async def to_out(db: AsyncSession, principal: Principal | None, p: AgentProposal) -> ProposalOut:
    try:
        spec: ToolSpec | None = get_tool(p.tool_key)
    except KeyError:
        spec = None
    try:
        agent_name = get_agent(p.agent_key).name
    except KeyError:
        agent_name = p.agent_key
    decidable = principal is not None and await can_decide(db, principal, p)
    show_args = decidable or (principal is not None and p.owner_user_id == principal.id)
    return ProposalOut(
        id=p.id, human_id=p.human_id, origin=p.origin, agent_key=p.agent_key, agent_name=agent_name,
        tool_key=p.tool_key, tool_title=spec.title if spec is not None else p.tool_key, title=p.title,
        preview=dict(p.preview or {}), risk=p.risk, rationale=p.rationale, confidence=p.confidence,
        evidence=list(p.evidence or []), target_type=p.target_type, target_id=p.target_id, status=p.status,
        edited=p.edited, assignee_user_id=p.assignee_user_id, route_role_key=p.route_role_key,
        owner_user_id=p.owner_user_id, expires_at=p.expires_at, decided_by=p.decided_by, decided_at=p.decided_at,
        decision_note=p.decision_note, executed_at=p.executed_at, error_code=p.error_code, run_id=p.run_id,
        can_decide=decidable, args=dict(p.args) if show_args else None,
        input_schema=_schema(spec) if decidable else None, created_at=p.created_at,
    )  # fmt: skip


async def _get(db: AsyncSession, proposal_id: uuid.UUID, *, lock: bool = False) -> AgentProposal:
    stmt = select(AgentProposal).where(AgentProposal.id == proposal_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Proposal not found")
    return row


async def get(ctx: RequestContext, db: AsyncSession, proposal_id: uuid.UUID) -> ProposalOut:
    principal = _principal(ctx)
    p = await _get(db, proposal_id)
    if not await _visible(db, principal, p):
        raise NotFound("Proposal not found")
    return await to_out(db, principal, p)


async def list_(ctx: RequestContext, db: AsyncSession, q: ProposalQuery) -> Page[ProposalOut]:
    principal = _principal(ctx)
    stmt = select(AgentProposal)
    role_keys: list[str] = []
    if principal.role_ids:
        rows = await db.execute(
            select(AgentProposal.route_role_key).where(AgentProposal.route_role_key.is_not(None)).distinct()
        )
        for key in rows.scalars():
            if await _role_member(db, principal, key):
                role_keys.append(key)
    mine = or_(
        AgentProposal.assignee_user_id == principal.id,
        AgentProposal.owner_user_id == principal.id,
        AgentProposal.route_role_key.in_(role_keys or [""]),
    )
    if q.mine or not principal.has("ai:govern"):
        stmt = stmt.where(mine)
    stmt = stmt.where(or_(AgentProposal.origin != "copilot", AgentProposal.owner_user_id == principal.id))
    stmt = stmt.where(AgentProposal.origin != "portal")  # Epic 77: portal Drafts are private to their portal owner
    if q.status:
        stmt = stmt.where(AgentProposal.status == q.status)
    if q.agent_key:
        stmt = stmt.where(AgentProposal.agent_key == q.agent_key)
    if q.tool_key:
        stmt = stmt.where(AgentProposal.tool_key == q.tool_key)
    if q.target_type:
        stmt = stmt.where(AgentProposal.target_type == q.target_type)
    if q.target_id:
        stmt = stmt.where(AgentProposal.target_id == q.target_id)
    if q.older_than_hours:
        stmt = stmt.where(AgentProposal.created_at < datetime.now(UTC) - timedelta(hours=q.older_than_hours))
    stmt = stmt.order_by(AgentProposal.created_at.desc(), AgentProposal.id)
    page = await paginate(db, stmt, PageParams(page=q.page, page_size=q.page_size))
    return Page[ProposalOut](
        items=[await to_out(db, principal, p) for p in page.items], total=page.total, page=page.page,
        page_size=page.page_size,
    )  # fmt: skip


# ================================================================ decisions
async def _write_back(db: AsyncSession, p: AgentProposal, decision: str, user_id: uuid.UUID | None) -> None:
    if p.run_id is None:
        return
    await db.execute(
        update(AIAction)
        .where(AIAction.agent_run_id == p.run_id)
        .values(decision=decision, decided_by=user_id, decided_at=datetime.now(UTC))
    )


def _decided(p: AgentProposal, outcome: str, user_id: uuid.UUID | None) -> ProposalDecided:
    return ProposalDecided(
        tenant_id=p.tenant_id, proposal_id=p.id, run_id=p.run_id, agent_key=p.agent_key, tool_key=p.tool_key,
        outcome=outcome, status=p.status, decided_by=user_id, target_type=p.target_type, target_id=p.target_id,
    )  # fmt: skip


def _validate_edit(spec: ToolSpec, original: BaseModel, edited: Mapping[str, Any]) -> BaseModel:
    try:
        args = spec.input_model.model_validate(dict(edited))
    except ValidationError as exc:
        raise ValidationFailed(
            "The edited proposal is not valid",
            code="proposal_edit_not_allowed",
            details={"errors": [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]},
        ) from exc
    if RISK_ORDER[spec.effective_risk(args)] > RISK_ORDER[spec.effective_risk(original)]:
        raise ValidationFailed("The edit raises the risk of the action", code="proposal_edit_not_allowed")
    return args


async def execute(
    ctx: RequestContext,
    db: AsyncSession,
    p: AgentProposal,
    approver: Principal,
    *,
    edited_args: Mapping[str, Any] | None,
    execution: str,
) -> None:
    """Shared approve / confirm path. Leaves ``p`` executed, execution_failed or stale (raises 409 on stale)."""
    from app.services.agent_runtime.state import ToolCtx

    try:
        spec = get_tool(p.tool_key)
    except KeyError as exc:
        raise ConflictError("The action is no longer available", code="tool_unavailable") from exc
    if spec.version != p.tool_version:
        raise ConflictError(
            "The action changed since the proposal", code="proposal_stale", details={"rerun_available": True}
        )
    original = spec.input_model.model_validate(dict(p.args))
    args = original
    edited = False
    if edited_args is not None and dict(edited_args) != dict(p.args):
        args = _validate_edit(spec, original, edited_args)
        edited = True
    run = await db.get(AgentRun, p.run_id) if p.run_id is not None else None
    subject = (
        (run.subject_type, run.subject_id)
        if run is not None and run.subject_type and run.subject_id
        else None
    )
    exec_ctx = RequestContext(
        request_id=ctx.request_id,
        tenant_id=p.tenant_id,
        principal=approver,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        proposal_id=p.id,
        agent_run_id=p.run_id,
        agent_key=p.agent_key,
    )
    tctx = ToolCtx(
        request=exec_ctx,
        db=db,
        actor=ActorRef("user", approver.id) if approver.type == "user" else ActorRef(approver.type, approver.id),  # type: ignore[arg-type]
        run_id=p.run_id,
        agent_key=p.agent_key,
        owner_user_id=p.owner_user_id,
        subject=subject,
        idempotency_key=f"proposal:{p.id}",
        execution=execution,  # type: ignore[arg-type]
    )
    # Staleness: the target version must be unchanged.
    if p.target_version is not None:
        current = await version_of(spec, tctx, args)
        if current != p.target_version:
            p.status = "stale"
            p.decided_by, p.decided_at = approver.id if approver.type == "user" else None, datetime.now(UTC)
            emit(db, _decided(p, "stale", approver.id))
            await db.flush()
            raise ConflictError(
                "The record changed since the proposal",
                code="proposal_stale",
                details={"rerun_available": True},
            )
    missing = [perm for perm in spec.permissions if not approver.has(perm)]
    if missing:
        raise Forbidden(
            "You do not have permission to perform this action", details={"permission": missing[0]}
        )
    if edited:
        p.args = args.model_dump(mode="json")
        p.args_sha256 = sha(p.args)
        p.edited = True
        assert spec.preview is not None
        p.preview = (await spec.preview(tctx, args)).to_json()
    p.status = "approved"
    # Epic 77: a portal approver (client_user / vendor_user / candidate) is not a user row (decided_by -> users).
    p.decided_by, p.decided_at = approver.id if approver.type == "user" else None, datetime.now(UTC)
    await db.flush()
    token = set_context(exec_ctx)
    try:
        async with db.begin_nested():
            outcome = await asyncio.wait_for(spec.handler(tctx, args), timeout=spec.timeout_s)
        p.status = "executed"
        p.executed_at = datetime.now(UTC)
        p.result = outcome.data.model_dump(mode="json")
    except DomainError as exc:
        p.status = "execution_failed"
        p.error_code = exc.code[:50]
    except TimeoutError:
        p.status = "execution_failed"
        p.error_code = "tool_timeout"
    finally:
        reset_context(token)
    await db.flush()


async def decide(
    ctx: RequestContext, db: AsyncSession, proposal_id: uuid.UUID, data: ProposalDecisionIn
) -> ProposalOut:
    require_human(actor_from_ctx(ctx))
    principal = _principal(ctx)
    p = await _get(db, proposal_id, lock=True)
    if not await _visible(db, principal, p):
        raise NotFound("Proposal not found")
    if p.origin == "copilot":
        raise ConflictError("Copilot drafts are confirmed, not decided", code="use_confirm")
    if p.status != "pending":
        raise ConflictError("This proposal is no longer pending", code="proposal_not_pending")
    if not await can_decide(db, principal, p):
        raise NotFound("Proposal not found")
    if p.expires_at <= datetime.now(UTC):
        raise ConflictError("This proposal has expired", code="proposal_not_pending")
    note = (data.note or "").strip() or None
    if data.decision == "reject":
        if note is None or len(note) < 3:
            raise ValidationFailed("A note is required to reject", code="note_required")
        p.status, p.decided_by, p.decided_at, p.decision_note = (
            "rejected",
            principal.id,
            datetime.now(UTC),
            note,
        )
        await _write_back(db, p, "rejected", principal.id)
        emit(db, _decided(p, "rejected", principal.id))
        audit.record("agent_proposal.rejected", "agent_proposal", p.id, session=db, tool_key=p.tool_key)
        await db.flush()
        return await to_out(db, principal, p)
    live = await owner_principal(db, p.tenant_id, principal.id)  # the approver's live permissions
    p.decision_note = note
    try:
        await execute(ctx, db, p, live, edited_args=data.edited_args, execution="approved_proposal")
    except ConflictError as exc:
        if exc.code == "proposal_stale":
            await db.commit()  # keep the stale status (the HTTP error rolls back otherwise)
        raise
    outcome = "edited_approved" if p.edited else "approved"
    await _write_back(db, p, "edited" if p.edited else "accepted", principal.id)
    emit(db, _decided(p, outcome, principal.id))
    audit.record(
        "agent_proposal.approved", "agent_proposal", p.id, session=db, tool_key=p.tool_key, status=p.status
    )
    await db.flush()
    return await to_out(db, principal, p)


async def bulk_decide(ctx: RequestContext, db: AsyncSession, data: BulkDecisionIn) -> list[ProposalOut]:
    require_human(actor_from_ctx(ctx))
    principal = _principal(ctx)
    rows = list(
        (await db.execute(select(AgentProposal).where(AgentProposal.id.in_(data.proposal_ids)))).scalars()
    )
    if len(rows) != len(set(data.proposal_ids)):
        raise NotFound("Proposal not found")
    demands = {
        dict(tuple(f) for f in (r.preview or {}).get("fields") or [] if len(f) == 2).get("demand_id")
        for r in rows
    }
    if len({(r.agent_key, r.tool_key) for r in rows}) != 1 or len(demands) != 1 or None in demands:
        raise ValidationFailed("Bulk decisions need the same agent, action and Demand", code="bulk_mixed")
    out: list[ProposalOut] = []
    for row in rows:
        try:
            async with db.begin_nested():
                out.append(
                    await decide(ctx, db, row.id, ProposalDecisionIn(decision=data.decision, note=data.note))
                )
        except DomainError:
            out.append(await to_out(db, principal, await _get(db, row.id)))
    return out


def _portal_owner(ctx: RequestContext) -> Principal | None:
    """Return the portal principal calling ``confirm_draft`` / ``discard_draft`` (Epic 77 portal routers), else None."""
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type in PORTAL_PRINCIPAL_TYPES:
        return principal
    return None


async def _own_portal_draft(
    db: AsyncSession, principal: Principal, proposal_id: uuid.UUID
) -> tuple[Principal, AgentProposal]:
    p = await _get(db, proposal_id, lock=True)
    if (
        p.origin != "portal"
        or p.owner_principal_type != principal.type
        or p.owner_principal_id != principal.id
        or (p.owner_org_id is not None and principal.org_id is not None and p.owner_org_id != principal.org_id)
    ):
        raise NotFound("Proposal not found")
    if p.status != "pending":
        raise ConflictError("This draft is no longer pending", code="proposal_not_pending")
    if p.expires_at <= datetime.now(UTC):
        raise ConflictError("This draft has expired", code="proposal_not_pending")
    return principal, p


async def _own_draft(
    ctx: RequestContext, db: AsyncSession, proposal_id: uuid.UUID
) -> tuple[Principal, AgentProposal]:
    require_human(actor_from_ctx(ctx))
    portal = _portal_owner(ctx)
    if portal is not None:
        return await _own_portal_draft(db, portal, proposal_id)
    principal = _principal(ctx)
    p = await _get(db, proposal_id, lock=True)
    if p.origin != "copilot" or p.owner_user_id != principal.id or principal.type != "user":
        raise NotFound("Proposal not found")
    if p.status != "pending":
        raise ConflictError("This draft is no longer pending", code="proposal_not_pending")
    if p.expires_at <= datetime.now(UTC):  # before the expiry sweep marks it
        raise ConflictError("This draft has expired", code="proposal_not_pending")
    return principal, p


async def confirm_draft(
    ctx: RequestContext,
    db: AsyncSession,
    proposal_id: uuid.UUID,
    *,
    edited_args: Mapping[str, Any] | None = None,
) -> ProposalOut:
    principal, p = await _own_draft(ctx, db, proposal_id)
    if principal.type in PORTAL_PRINCIPAL_TYPES:
        return await _confirm_portal_draft(ctx, db, p, edited_args)
    live = await owner_principal(db, p.tenant_id, principal.id)
    await execute(ctx, db, p, live, edited_args=edited_args, execution="copilot_confirm")
    await _write_back(db, p, "edited" if p.edited else "accepted", principal.id)
    emit(db, _decided(p, "edited_approved" if p.edited else "approved", principal.id))
    await db.flush()
    return await to_out(db, principal, p)


async def _confirm_portal_draft(
    ctx: RequestContext,
    db: AsyncSession,
    p: AgentProposal,
    edited_args: Mapping[str, Any] | None,
) -> ProposalOut:
    """Epic 66 (§0.15): the portal owner confirms its Draft. The tool executes as the live portal principal, whose
    rights are the agent's permission ceiling ∩ its portal permissions (a deactivated portal user fails closed)."""
    from app.services.agent_runtime.principals import OwnerUnavailable, portal_principal, portal_rights

    try:
        live_portal = await portal_principal(
            db, p.tenant_id, str(p.owner_principal_type), p.owner_principal_id, p.owner_org_kind, p.owner_org_id
        )
    except OwnerUnavailable as exc:
        raise NotFound("Proposal not found") from exc
    try:
        ceiling = get_agent(p.agent_key).permission_ceiling
    except KeyError:
        ceiling = {}
    import dataclasses

    live = dataclasses.replace(live_portal, permissions=portal_rights(live_portal, ceiling))
    await execute(ctx, db, p, live, edited_args=edited_args, execution="copilot_confirm")
    await _write_back(db, p, "edited" if p.edited else "accepted", None)
    emit(db, _decided(p, "edited_approved" if p.edited else "approved", None))
    await db.flush()
    return await to_out(db, None, p)


async def discard_draft(ctx: RequestContext, db: AsyncSession, proposal_id: uuid.UUID) -> ProposalOut:
    principal, p = await _own_draft(ctx, db, proposal_id)
    if principal.type in PORTAL_PRINCIPAL_TYPES:  # Epic 66: portal owners (decided_by references users)
        p.status, p.decided_at = "discarded", datetime.now(UTC)
        await _write_back(db, p, "rejected", None)
        emit(db, _decided(p, "discarded", None))
        await db.flush()
        return await to_out(db, None, p)
    p.status, p.decided_by, p.decided_at = "discarded", principal.id, datetime.now(UTC)
    await _write_back(db, p, "rejected", principal.id)
    emit(db, _decided(p, "discarded", principal.id))
    await db.flush()
    return await to_out(db, principal, p)


async def expire_due(db: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    rows = list(
        (
            await db.execute(
                select(AgentProposal).where(
                    AgentProposal.status == "pending", AgentProposal.expires_at <= now
                )
            )
        ).scalars()
    )
    for p in rows:
        p.status = "expired"
        emit(db, _decided(p, "expired", None))
    await db.flush()
    return len(rows)


async def expiring_soon(db: AsyncSession, now: datetime | None = None) -> list[AgentProposal]:
    now = now or datetime.now(UTC)
    return list(
        (
            await db.execute(
                select(AgentProposal).where(
                    AgentProposal.status == "pending",
                    AgentProposal.origin == "agent",
                    AgentProposal.expires_at > now,
                    AgentProposal.expires_at <= now + timedelta(hours=24),
                )
            )
        ).scalars()
    )


__all__ = [
    "PORTAL_OWNER_ROUTE",
    "PORTAL_PRINCIPAL_TYPES",
    "bulk_decide",
    "can_decide",
    "confirm_draft",
    "create",
    "create_draft_for",
    "decide",
    "discard_draft",
    "execute",
    "expire_due",
    "expiring_soon",
    "get",
    "list_",
    "sha",
    "target_of",
    "to_out",
    "version_of",
]
