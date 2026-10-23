"""Generic approval engine (story 4.2; extended by Epic 13, AD-24): demands, offers, margin override and every
Phase 2 approval.

* ``start`` snapshots either the subject type's active chain (Phase 1 path) or explicit ``steps``
  (``StepSpec``), resolving every step's approver at snapshot through the approver-resolver registry
  (``user`` / ``role`` / ``finance_role`` built in; ``client_user`` / ``client_any`` from ``external_auth``;
  ``placement_account_manager`` from ``placements``). Unresolvable -> 422 ``approver_unresolved``. Steps whose
  condition is false are recorded ``skipped``; no applicable step -> ``auto_approved``.
* Activating a step whose approver belongs to an external organization grants that organization visibility
  of the request (``org_grants``, kept after completion).
* ``decide`` accepts only approver actors (``user``, ``client_user``, ``secure_link``, ``recorded_by_user``);
  ``ai`` / ``workflow`` / ``system`` / ``api_key`` (and ``candidate`` / ``vendor_user``) get 403
  ``human_only_action``. The actor must match the current approver; the decision row records the full
  ``ActorRef``; subject callbacks (``on_step_decided`` for every human step, then ``on_final_decided``) run
  synchronously in the deciding transaction with that ``ActorRef`` -- an exception rolls the decision back.
* Subjects register callbacks (``register_approval_subject``; Phase 1 ``register_subject`` maps onto it) so the
  engine never imports its subjects (AD-1).
"""

import contextlib
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from loguru import logger
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import org_grants
from app.core.actors import APPROVER_KINDS, ActorKind, ActorRef, actor_from_ctx, human_only_action
from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit, subscribe
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal, can_view_compensation, check, mask_compensation
from app.events.approvals import ApprovalCompleted, ApprovalStepActivated
from app.models.approvals import ApprovalChain, ApprovalChainStep, ApprovalDecision, ApprovalRequest
from app.schemas.approvals import (
    ChainIn,
    ChainOut,
    ChainStepOut,
    DecideIn,
    DecisionOut,
    RequestOut,
    SubjectSummary,
)
from app.services import directory_service as directory
from app.services.approvals import conditions
from app.services.approvals.registry import (
    Describe,
    EscalationSpec,
    FinalDecision,
    ResolvedApprover,
    StepDecision,
    StepSpec,
    approver_resolver,
    delegate_resolver,
    register_approval_subject,
    register_approver_resolver,
    subject,
    subjects,
    unregister_approval_subject,
)

__all__ = ["ActorKind"]

SUBJECT_PERMISSIONS: dict[str, str] = {
    "demand": "demand:approve",
    "offer": "offer:approve",
    "margin_override": "offer:approve",
}
RECORD_OFFLINE = "approval:record_offline"

OnApproved = Callable[[RequestContext, AsyncSession, uuid.UUID], Awaitable[None]]
# (ctx, db, subject_id, outcome "rejected"|"changes_requested", comment)
OnReturned = Callable[[RequestContext, AsyncSession, uuid.UUID, str, str], Awaitable[None]]


class _SubjectsCompat(dict[str, Any]):
    """Phase 1 tests read / restore ``service._subjects[kind]``; backed by the subject registry since Epic 13."""

    def get(self, key: str, default: Any = None) -> Any:
        found = subject(key)
        return found if found is not None else default

    def __getitem__(self, key: str) -> Any:
        found = subject(key)
        if found is None:
            raise KeyError(key)
        return found

    def __setitem__(self, key: str, value: Any) -> None:
        from app.services.approvals.registry import put_subject_definition

        put_subject_definition(value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and subject(key) is not None


_subjects = _SubjectsCompat()

# The request whose ``on_step_decided`` callback is running (``skip_current_step`` is only valid there).
_STEP_CALLBACK: ContextVar[uuid.UUID | None] = ContextVar("talent_os_approval_step_callback", default=None)


def register_subject(
    subject_type: str, on_approved: OnApproved, on_returned: OnReturned, describe: Describe | None = None
) -> None:
    """Phase 1 compatibility: maps onto ``register_approval_subject`` with the ``SUBJECT_PERMISSIONS`` key."""
    if subject_type not in SUBJECT_PERMISSIONS:
        msg = f"unknown approval subject type {subject_type!r}"
        raise ValueError(msg)

    async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: FinalDecision) -> None:
        if decision.outcome == "approved":
            await on_approved(ctx, db, decision.subject_id)
        elif decision.outcome in ("rejected", "changes_requested"):
            await on_returned(ctx, db, decision.subject_id, decision.outcome, decision.comment or "")

    register_approval_subject(
        subject_type,
        on_final_decided=on_final_decided,
        describe=describe,
        permission=SUBJECT_PERMISSIONS[subject_type],
    )


def unregister_subject(subject_type: str) -> None:
    unregister_approval_subject(subject_type)


@dataclasses.dataclass(frozen=True, slots=True)
class StartResult:
    status: Literal["auto_approved", "pending"]
    request_id: uuid.UUID | None


def _now() -> datetime:
    return datetime.now(UTC)


def actor_kind_of(ctx: RequestContext) -> ActorKind:
    """Delegates to ``core.actors.actor_from_ctx`` (Epic 13)."""
    return actor_from_ctx(ctx).kind


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise NotFound("Resource not found")
    return principal


def human_only() -> Forbidden:
    return human_only_action()


def _not_current_approver() -> Forbidden:
    return Forbidden("You are not the approver of the current step", code="not_current_approver")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    return str(value)


def _jsonable_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in facts.items():
        out[key] = None if value is None else (value if isinstance(value, bool | int | str) else str(value))
    return out


def _uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


# ================================================================ built-in approver resolvers
def _unresolved(step: str) -> ValidationFailed:
    return ValidationFailed("The approver of a step could not be resolved", code="approver_unresolved", details={"step": step})


async def _resolve_user(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,  # ruff: ignore[unused-function-argument]
    ref: Mapping[str, Any],
    facts: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
) -> ResolvedApprover:
    user_id = _uuid(ref.get("user_id"))
    if user_id is None:
        raise LookupError("user_id")
    return ResolvedApprover("user", user_id, None, None, None, "user")


async def _resolve_role(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ref: Mapping[str, Any],
    facts: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
) -> ResolvedApprover:
    role_id = _uuid(ref.get("role_id"))
    if role_id is None and ref.get("role_key"):
        role_id = await directory.role_id_by_key(db, str(ref["role_key"]))
    if role_id is None:
        raise LookupError("role")
    return ResolvedApprover("role", None, role_id, None, None, "role")


async def _resolve_finance_role(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ref: Mapping[str, Any],
    facts: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
) -> ResolvedApprover:
    role_id = await directory.role_id_by_key(db, str(ref.get("role_key") or "finance_admin"))
    if role_id is None:
        role_id = await directory.role_id_by_key(db, "tenant_admin")
    if role_id is None:
        raise LookupError("finance_role")
    return ResolvedApprover("role", None, role_id, None, None, "finance")


register_approver_resolver("user", _resolve_user)
register_approver_resolver("role", _resolve_role)
register_approver_resolver("finance_role", _resolve_finance_role)


async def _resolve(
    ctx: RequestContext, db: AsyncSession, name: str, kind: str, ref: Mapping[str, Any], facts: Mapping[str, Any]
) -> ResolvedApprover:
    resolver = approver_resolver(kind)
    if resolver is None:
        raise _unresolved(name)
    try:
        resolved = await resolver(ctx, db, ref, facts)
    except (LookupError, ValueError, DomainError) as exc:
        raise _unresolved(name) from exc
    if resolved is None:  # defensive: resolvers return a ResolvedApprover
        raise _unresolved(name)
    return resolved


# ================================================================ chains (settings)
async def _validate_steps(db: AsyncSession, data: ChainIn) -> None:
    user_ids: set[uuid.UUID] = set()
    role_ids: set[uuid.UUID] = set()
    for i, step in enumerate(data.steps, start=1):
        kind = step.approver_kind or ("user" if step.approver_user_id else "role")
        if kind in ("user", "role"):
            if (step.approver_user_id is None) == (step.approver_role_id is None):
                raise ValidationFailed(
                    "Each step needs exactly one approver (a user or a role)",
                    code="invalid_approver",
                    details={"step": i},
                )
        elif approver_resolver(kind) is None or not step.approver_ref:
            raise ValidationFailed("Unknown approver kind", code="invalid_approver", details={"step": i})
        try:
            conditions.validate(step.condition)
        except conditions.ConditionError as exc:
            raise ValidationFailed(
                f"Invalid condition on step {i}: {exc}", code="invalid_condition", details={"step": i}
            ) from exc
        if step.approver_user_id:
            user_ids.add(step.approver_user_id)
        if step.approver_role_id:
            role_ids.add(step.approver_role_id)
    missing_users = user_ids - await directory.active_user_ids(db, user_ids)
    missing_roles = role_ids - await directory.existing_role_ids(db, role_ids)
    if missing_users or missing_roles:
        raise ValidationFailed(
            "Unknown approver",
            code="invalid_reference",
            details={
                "user_ids": sorted(map(str, missing_users)),
                "role_ids": sorted(map(str, missing_roles)),
            },
        )


async def _chain_out(db: AsyncSession, chain: ApprovalChain) -> ChainOut:
    steps = list(
        (
            await db.execute(
                select(ApprovalChainStep)
                .where(ApprovalChainStep.chain_id == chain.id)
                .order_by(ApprovalChainStep.position)
            )
        ).scalars()
    )
    users = await directory.user_names(db, [s.approver_user_id for s in steps if s.approver_user_id])
    roles = await directory.role_names(db, [s.approver_role_id for s in steps if s.approver_role_id])
    return ChainOut(
        id=chain.id,
        subject_type=chain.subject_type,
        name=chain.name,
        is_active=chain.is_active,
        created_at=chain.created_at,
        updated_at=chain.updated_at,
        steps=[
            ChainStepOut(
                id=s.id,
                position=s.position,
                name=s.name,
                approver_user_id=s.approver_user_id,
                approver_role_id=s.approver_role_id,
                approver_name=(
                    users.get(s.approver_user_id) if s.approver_user_id else roles.get(s.approver_role_id)  # type: ignore[arg-type]
                ),
                condition=s.condition,
                approver_kind=s.approver_kind,
                approver_ref=s.approver_ref,
            )
            for s in steps
        ],
    )


def _active_exists() -> ConflictError:
    return ConflictError("Another active chain exists for this subject type", code="active_chain_exists")


async def _ensure_single_active(db: AsyncSession, subject_type: str, except_id: uuid.UUID | None) -> None:
    stmt = select(ApprovalChain.id).where(
        ApprovalChain.subject_type == subject_type, ApprovalChain.is_active.is_(True)
    )
    if except_id is not None:
        stmt = stmt.where(ApprovalChain.id != except_id)
    if (await db.execute(stmt)).first() is not None:
        raise _active_exists()


async def _write_steps(ctx: RequestContext, db: AsyncSession, chain: ApprovalChain, data: ChainIn) -> None:
    for i, step in enumerate(data.steps, start=1):
        kind = step.approver_kind or ("user" if step.approver_user_id else "role")
        db.add(
            ApprovalChainStep(
                id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                chain_id=chain.id,
                position=i,
                name=step.name,
                approver_user_id=step.approver_user_id if kind == "user" else None,
                approver_role_id=step.approver_role_id if kind == "role" else None,
                approver_kind=kind,
                approver_ref=step.approver_ref if kind not in ("user", "role") else None,
                condition=step.condition,
            )
        )
    await db.flush()


async def list_chains(ctx: RequestContext, db: AsyncSession) -> list[ChainOut]:  # ruff: ignore[unused-function-argument]
    chains = (
        await db.execute(select(ApprovalChain).order_by(ApprovalChain.subject_type, ApprovalChain.created_at))
    ).scalars()
    return [await _chain_out(db, c) for c in chains]


async def _get_chain(db: AsyncSession, chain_id: uuid.UUID) -> ApprovalChain:
    chain = await db.get(ApprovalChain, chain_id)
    if chain is None:
        raise NotFound("Approval chain not found")
    return chain


async def get_chain(ctx: RequestContext, db: AsyncSession, chain_id: uuid.UUID) -> ChainOut:  # ruff: ignore[unused-function-argument]
    return await _chain_out(db, await _get_chain(db, chain_id))


async def create_chain(ctx: RequestContext, db: AsyncSession, data: ChainIn) -> ChainOut:
    await _validate_steps(db, data)
    if data.is_active:
        await _ensure_single_active(db, data.subject_type, None)
    chain = ApprovalChain(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        subject_type=data.subject_type,
        name=data.name,
        is_active=data.is_active,
    )
    try:
        async with db.begin_nested():
            db.add(chain)
            await db.flush()
    except IntegrityError as exc:
        raise _active_exists() from exc
    await _write_steps(ctx, db, chain, data)
    await db.refresh(chain)
    return await _chain_out(db, chain)


async def update_chain(ctx: RequestContext, db: AsyncSession, chain_id: uuid.UUID, data: ChainIn) -> ChainOut:
    chain = await _get_chain(db, chain_id)
    await _validate_steps(db, data)
    if data.is_active:
        await _ensure_single_active(db, data.subject_type, chain.id)
    chain.subject_type = data.subject_type
    chain.name = data.name
    chain.is_active = data.is_active
    for step in (
        await db.execute(select(ApprovalChainStep).where(ApprovalChainStep.chain_id == chain.id))
    ).scalars():
        await db.delete(step)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _active_exists() from exc
    await _write_steps(ctx, db, chain, data)
    await db.refresh(chain)
    return await _chain_out(db, chain)


async def delete_chain(ctx: RequestContext, db: AsyncSession, chain_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    chain = await _get_chain(db, chain_id)
    await db.delete(chain)
    await db.flush()


# ================================================================ requests
def _escalation_json(spec: EscalationSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {
        "after_business_hours": str(spec.after_business_hours),
        "action": spec.action,
        "target_kind": spec.target_kind,
        "target_ref": str(spec.target_ref) if spec.target_ref else None,
        "repeat_every_hours": str(spec.repeat_every_hours) if spec.repeat_every_hours is not None else None,
        "max_repeats": spec.max_repeats,
    }


def _snapshot_entry(
    position: int,
    name: str,
    declared_kind: str,
    ref: Mapping[str, Any] | None,
    resolved: ResolvedApprover,
    condition: Mapping[str, Any] | None,
    escalation: EscalationSpec | None,
) -> dict[str, Any]:
    return {
        "position": position,
        "name": name,
        "approver_kind": declared_kind,
        "resolved_kind": resolved.kind,
        "approver_user_id": str(resolved.user_id) if resolved.user_id else None,
        "approver_role_id": str(resolved.role_id) if resolved.role_id else None,
        "approver_client_user_id": str(resolved.client_user_id) if resolved.client_user_id else None,
        "approver_ref": _jsonable(ref) if ref else None,
        "org": [resolved.org[0], str(resolved.org[1])] if resolved.org else None,
        "display": resolved.display,
        "condition": dict(condition) if condition is not None else None,
        "escalation": _escalation_json(escalation),
    }


async def _snapshot_chain(
    ctx: RequestContext, db: AsyncSession, steps: Iterable[ApprovalChainStep], facts: Mapping[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in steps:
        if s.approver_kind == "user" or (s.approver_kind is None and s.approver_user_id):  # type: ignore[unreachable]
            resolved = ResolvedApprover("user", s.approver_user_id, None, None, None, "user")
            kind = "user"
        elif s.approver_kind == "role" or (s.approver_kind is None and s.approver_role_id):  # type: ignore[unreachable]
            resolved = ResolvedApprover("role", None, s.approver_role_id, None, None, "role")
            kind = "role"
        else:
            kind = str(s.approver_kind)
            resolved = await _resolve(ctx, db, s.name, kind, s.approver_ref or {}, facts)
        out.append(_snapshot_entry(s.position, s.name, kind, s.approver_ref, resolved, s.condition, None))
    return out


async def _snapshot_explicit(
    ctx: RequestContext, db: AsyncSession, steps: Sequence[StepSpec], facts: Mapping[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, spec in enumerate(steps, start=1):
        if spec.condition is not None:
            try:
                conditions.validate(dict(spec.condition))
            except conditions.ConditionError as exc:
                raise ValidationFailed(
                    f"Invalid condition on step {i}: {exc}", code="invalid_condition", details={"step": spec.name}
                ) from exc
        resolved = await _resolve(ctx, db, spec.name, spec.approver_kind, spec.approver_ref, facts)
        out.append(
            _snapshot_entry(i, spec.name, spec.approver_kind, spec.approver_ref, resolved, spec.condition, spec.escalation)
        )
    return out


def _step_at(request: ApprovalRequest, position: int | None) -> dict[str, Any] | None:
    if position is None:
        return None
    for step in request.chain_snapshot:
        if step["position"] == position:
            return step
    return None


def _resolved_kind(step: Mapping[str, Any]) -> str:
    kind = step.get("resolved_kind")
    if kind:
        return str(kind)
    return "user" if step.get("approver_user_id") else "role"


def _record(
    ctx: RequestContext,
    db: AsyncSession,
    request: ApprovalRequest,
    step: Mapping[str, Any],
    outcome: str,
    *,
    actor: ActorRef | None = None,
    actor_id: uuid.UUID | None = None,
    comment: str | None = None,
    evaluated: dict[str, Any] | None = None,
) -> None:
    db.add(
        ApprovalDecision(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id or request.tenant_id,
            request_id=request.id,
            position=int(step["position"]),
            step_name=str(step["name"]),
            outcome=outcome,
            actor_id=actor_id,
            comment=comment,
            evaluated_condition=evaluated,
            decided_at=_now(),
            actor_kind=actor.kind if actor is not None else "system",
            attributed_kind=actor.attributed_to[0] if actor is not None and actor.attributed_to else None,
            attributed_id=actor.attributed_to[1] if actor is not None and actor.attributed_to else None,
            on_behalf_of=actor.on_behalf_of if actor is not None else None,
            evidence_file_id=actor.evidence_file_id if actor is not None else None,
            secure_link_id=actor.id if actor is not None and actor.kind == "secure_link" else None,
        )
    )


def _evaluation(step: Mapping[str, Any], facts: Mapping[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    condition = step.get("condition")
    applies = conditions.evaluate(condition, facts)
    if condition is None:
        return applies, None
    used = {f: facts.get(f) for f in conditions.referenced_fields(condition)}
    return applies, {"condition": condition, "facts": used, "applies": applies}


def _clear_current(request: ApprovalRequest) -> None:
    request.current_position = None
    request.current_approver_user_id = None
    request.current_approver_role_id = None
    request.current_approver_kind = None
    request.current_approver_client_user_id = None
    request.current_step_activated_at = None


async def _activate_from(
    ctx: RequestContext, db: AsyncSession, request: ApprovalRequest, start_position: int
) -> bool:
    """Move to the first applicable step at/after ``start_position`` (recording skips). False if none."""
    for step in sorted(request.chain_snapshot, key=lambda s: int(s["position"])):
        if int(step["position"]) < start_position:
            continue
        applies, evaluated = _evaluation(step, request.facts)
        if not applies:
            _record(ctx, db, request, step, "skipped", evaluated=evaluated)
            continue
        kind = _resolved_kind(step)
        request.current_position = int(step["position"])
        request.current_approver_user_id = _uuid(step.get("approver_user_id"))
        request.current_approver_role_id = _uuid(step.get("approver_role_id"))
        request.current_approver_client_user_id = _uuid(step.get("approver_client_user_id"))
        request.current_approver_kind = kind
        request.current_step_activated_at = _now()
        await db.flush()
        org = step.get("org")
        if org:
            await org_grants.grant(
                db, str(org[0]), uuid.UUID(str(org[1])), "approval_request", request.id, ActorRef("system", None)
            )
        approvers: set[uuid.UUID] = set()
        if request.current_approver_user_id:
            approvers.add(request.current_approver_user_id)
        if request.current_approver_role_id:
            approvers |= await directory.user_ids_with_role(db, request.current_approver_role_id)
        emit(
            db,
            ApprovalStepActivated(
                tenant_id=request.tenant_id,
                request_id=request.id,
                subject_type=request.subject_type,
                subject_id=request.subject_id,
                position=int(step["position"]),
                approver_user_ids=tuple(sorted(approvers)),
                approver_role_id=request.current_approver_role_id,
                approver_kind=kind,
                approver_client_user_id=request.current_approver_client_user_id,
            ),
        )
        return True
    _clear_current(request)
    return False


def _complete(db: AsyncSession, request: ApprovalRequest, status: str) -> None:
    request.status = status
    request.completed_at = _now()
    _clear_current(request)
    emit(
        db,
        ApprovalCompleted(
            tenant_id=request.tenant_id,
            request_id=request.id,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            status=status,
        ),
    )


async def cancel_open(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID,
) -> None:
    rows = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.subject_type == subject_type,
            ApprovalRequest.subject_id == subject_id,
            ApprovalRequest.status == "pending",
        )
    )
    for request in rows.scalars():
        _complete(db, request, "cancelled")
    await db.flush()


def _known_subject(subject_type: str) -> None:
    if subject_type not in SUBJECT_PERMISSIONS and subject(subject_type) is None:
        msg = f"unknown approval subject type {subject_type!r}"
        raise ValueError(msg)


async def _auto_approved_callback(
    ctx: RequestContext, db: AsyncSession, subject_type: str, subject_id: uuid.UUID, request_id: uuid.UUID | None
) -> None:
    definition = subject(subject_type)
    if definition is not None and definition.auto_approve_calls_back:
        await definition.on_final_decided(
            ctx,
            db,
            FinalDecision(
                request_id=request_id,
                subject_type=subject_type,
                subject_id=subject_id,
                outcome="auto_approved",
                actor=None,
                comment=None,
            ),
        )


async def start(
    ctx: RequestContext,
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID,
    facts: Mapping[str, Any],
    *,
    steps: Sequence[StepSpec] | None = None,
    initiated_by: ActorRef | None = None,
) -> StartResult:
    _known_subject(subject_type)
    await cancel_open(ctx, db, subject_type, subject_id)
    chain_id: uuid.UUID | None = None
    if steps is not None:
        if not steps:
            raise ValidationFailed("At least one approval step is required", code="approver_unresolved", details={"step": None})
        snapshot = await _snapshot_explicit(ctx, db, steps, facts)
        steps_source = "explicit"
    else:
        chain = (
            await db.execute(
                select(ApprovalChain).where(
                    ApprovalChain.subject_type == subject_type, ApprovalChain.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if chain is None:
            await _auto_approved_callback(ctx, db, subject_type, subject_id, None)
            return StartResult("auto_approved", None)
        chain_steps = (
            await db.execute(
                select(ApprovalChainStep)
                .where(ApprovalChainStep.chain_id == chain.id)
                .order_by(ApprovalChainStep.position)
            )
        ).scalars()
        snapshot = await _snapshot_chain(ctx, db, chain_steps, facts)
        chain_id = chain.id
        steps_source = "chain"
    initiator = initiated_by or actor_from_ctx(ctx)
    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        chain_id=chain_id,
        chain_snapshot=snapshot,
        facts=_jsonable_facts(facts),
        status="pending",
        submitted_by=getattr(ctx.principal, "id", None),
        submitted_at=_now(),
        steps_source=steps_source,
        initiated_by_kind=initiator.kind,
    )
    db.add(request)
    await db.flush()
    if await _activate_from(ctx, db, request, 1):
        return StartResult("pending", request.id)
    _complete(db, request, "approved")
    await db.flush()
    await _auto_approved_callback(ctx, db, subject_type, subject_id, request.id)
    return StartResult("auto_approved", request.id)


# ---------------------------------------------------------------- decide: approver matching
def _is_current_approver(principal: Principal, request: ApprovalRequest) -> bool:
    if request.current_approver_user_id is not None:
        return request.current_approver_user_id == principal.id
    return (
        request.current_approver_role_id is not None
        and request.current_approver_role_id in principal.role_ids
    )


def _subject_permission(subject_type: str) -> str | None:
    definition = subject(subject_type)
    if definition is not None and definition.permission:
        return definition.permission
    return SUBJECT_PERMISSIONS.get(subject_type)


async def _visible_placement(ctx: RequestContext, db: AsyncSession, request: ApprovalRequest) -> bool:
    placement_id = _uuid(request.facts.get("placement_id"))
    if placement_id is None:
        return True
    from app.core.client_scope import visible_placement_ids

    visible = await visible_placement_ids(ctx, db)
    return visible is None or placement_id in visible


async def _client_user_matches_any(
    db: AsyncSession, step: Mapping[str, Any], client_user_id: uuid.UUID, permissions: frozenset[str] | None
) -> bool:
    """A Client User of the step's client who holds the step's portal permission (``client_any``)."""
    org = step.get("org") or [None, None]
    ref = step.get("approver_ref") or {}
    wanted = ref.get("portal_permission")
    if permissions is None:
        row = (
            await db.execute(
                text("SELECT client_id, portal_permissions, status FROM client_users WHERE id = :id"),
                {"id": client_user_id},
            )
        ).first()
        if row is None or row.status != "active" or str(row.client_id) != str(org[1]):
            return False
        permissions = frozenset(row.portal_permissions or ())
    return wanted is None or wanted in permissions


async def _attributed_matches(
    db: AsyncSession, request: ApprovalRequest, step: Mapping[str, Any], attributed: tuple[str, uuid.UUID] | None
) -> bool:
    """Is ``attributed`` (the person a secure link / offline record speaks for) the current approver?"""
    if attributed is None:
        return False
    who_kind, who_id = attributed[0], uuid.UUID(str(attributed[1]))
    kind = request.current_approver_kind or _resolved_kind(step)
    if kind == "user":
        return who_kind == "user" and request.current_approver_user_id == who_id
    if kind == "role":
        return (
            who_kind == "user"
            and request.current_approver_role_id is not None
            and who_id in await directory.user_ids_with_role(db, request.current_approver_role_id)
        )
    if kind == "client_user":
        if who_kind == "client_user":
            return request.current_approver_client_user_id == who_id
        if who_kind == "contact":
            return str((step.get("approver_ref") or {}).get("contact_id") or "") == str(who_id)
        return False
    if kind == "client_any":
        return who_kind == "client_user" and await _client_user_matches_any(db, step, who_id, None)
    return False


async def _delegated_for(ctx: RequestContext, db: AsyncSession, actor: ActorRef, principal: Any) -> uuid.UUID | None:
    """Who the deciding principal acts for (``on_behalf_of``), after checking the delegate registry."""
    if actor.on_behalf_of is None:
        return None
    resolver = delegate_resolver(str(getattr(principal, "type", "")))
    if resolver is None:
        raise _not_current_approver()
    allowed = await resolver(ctx, db, principal.id, _now().date())
    if actor.on_behalf_of not in allowed:
        raise _not_current_approver()
    return actor.on_behalf_of


async def _check_approver(
    ctx: RequestContext, db: AsyncSession, request: ApprovalRequest, step: Mapping[str, Any], actor: ActorRef
) -> uuid.UUID | None:
    """Raise unless ``actor`` may decide the current step; returns the ``actor_id`` to record."""
    principal = ctx.principal
    kind = request.current_approver_kind or _resolved_kind(step)
    permission = _subject_permission(request.subject_type)
    if actor.kind == "user":
        if not isinstance(principal, Principal) or principal.type != "user":
            raise NotFound("Resource not found")
        delegated = await _delegated_for(ctx, db, actor, principal)
        if delegated is not None:
            if not await _attributed_matches(db, request, step, ("user", delegated)):
                raise _not_current_approver()
        elif kind not in ("user", "role") or not _is_current_approver(principal, request):
            raise _not_current_approver()
        if permission:
            check(principal, permission)
        return principal.id
    if actor.kind == "client_user":
        if not isinstance(principal, Principal) or principal.type != "client_user" or principal.id != actor.id:
            raise _not_current_approver()
        delegated = await _delegated_for(ctx, db, actor, principal)
        if kind == "client_user":
            if (delegated or principal.id) != request.current_approver_client_user_id:
                raise _not_current_approver()
        elif kind == "client_any":
            org = step.get("org") or [None, None]
            if principal.org_id is None or str(principal.org_id) != str(org[1]):
                raise _not_current_approver()
            target = delegated or principal.id
            perms = principal.portal_permissions if delegated is None else None
            if not await _client_user_matches_any(db, step, target, perms):
                raise _not_current_approver()
            if not await _visible_placement(ctx, db, request):
                raise _not_current_approver()
        else:
            raise _not_current_approver()
        return principal.id
    if actor.kind == "secure_link":
        if actor.id is None or not await _attributed_matches(db, request, step, actor.attributed_to):
            raise _not_current_approver()
        return actor.id
    if actor.kind == "recorded_by_user":
        if not isinstance(principal, Principal) or principal.type != "user" or principal.id != actor.id:
            raise _not_current_approver()
        if actor.evidence_file_id is None:
            raise ValidationFailed("Attach the evidence of the offline approval", code="evidence_required")
        check(principal, RECORD_OFFLINE)
        if permission:
            check(principal, permission)
        if not await _attributed_matches(db, request, step, actor.attributed_to):
            raise _not_current_approver()
        return principal.id
    raise human_only()


@contextlib.contextmanager
def _in_step_callback(request_id: uuid.UUID) -> Iterator[None]:
    token = _STEP_CALLBACK.set(request_id)
    try:
        yield
    finally:
        _STEP_CALLBACK.reset(token)


async def decide(
    ctx: RequestContext,
    db: AsyncSession,
    request_id: uuid.UUID,
    data: DecideIn,
    *,
    actor: ActorRef | None = None,
    actor_kind: ActorKind | None = None,
) -> RequestOut:
    if actor_kind is not None and actor_kind != "user":  # Phase 1 compat keyword
        raise human_only()
    actor = actor or actor_from_ctx(ctx)
    if actor.kind not in APPROVER_KINDS:
        raise human_only()
    if actor.kind == "user":
        _principal(ctx)  # Phase 1: non-user principals never see requests (404)
    request = (
        await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update())
    ).scalar_one_or_none()
    if request is None:
        raise NotFound("Approval request not found")
    if request.status != "pending" or request.current_position is None:
        raise ConflictError("This approval request is no longer pending", code="request_not_pending")
    step = _step_at(request, request.current_position)
    assert step is not None
    actor_id = await _check_approver(ctx, db, request, step, actor)
    comment = (data.comment or "").strip() or None
    if data.action in ("reject", "request_changes") and comment is None:
        raise ValidationFailed("A comment is required", code="comment_required")
    payload = data.subject_payload
    definition = subject(request.subject_type)
    position = int(step["position"])
    if data.action == "approve":
        outcome: Literal["approved", "rejected", "changes_requested"] = "approved"
        _record(ctx, db, request, step, "approved", actor=actor, actor_id=actor_id, comment=comment)
        is_final = not await _activate_from(ctx, db, request, position + 1)
        if is_final:
            _complete(db, request, "approved")
    else:
        outcome = "rejected" if data.action == "reject" else "changes_requested"
        _record(ctx, db, request, step, outcome, actor=actor, actor_id=actor_id, comment=comment)
        is_final = True
        _complete(db, request, outcome)
    await db.flush()
    if definition is None:
        if request.status != "pending":
            logger.warning("No approval subject registered for {}", request.subject_type)
    else:
        if definition.on_step_decided is not None:
            with _in_step_callback(request.id):
                await definition.on_step_decided(
                    ctx,
                    db,
                    StepDecision(
                        request_id=request.id,
                        subject_type=request.subject_type,
                        subject_id=request.subject_id,
                        position=position,
                        step_name=str(step["name"]),
                        outcome=outcome,
                        actor=actor,
                        comment=comment,
                        is_final=is_final,
                        payload=payload,
                    ),
                )
            await db.flush()
        if request.status != "pending":
            await definition.on_final_decided(
                ctx,
                db,
                FinalDecision(
                    request_id=request.id,
                    subject_type=request.subject_type,
                    subject_id=request.subject_id,
                    outcome=request.status,  # type: ignore[arg-type]
                    actor=actor,
                    comment=comment,
                    payload=payload,
                ),
            )
    await db.flush()
    return await _request_out(ctx, db, request)


async def skip_current_step(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, *, reason: str) -> None:
    """Skip the now-current step (``actor_kind='system'``, outcome ``skipped`` -- never a human approval) and
    activate the next one. Only valid inside an ``on_step_decided`` callback of the same request."""
    if _STEP_CALLBACK.get() != request_id:
        msg = "skip_current_step is only valid inside an on_step_decided callback of the same request"
        raise RuntimeError(msg)
    request = (
        await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update())
    ).scalar_one()
    if request.status != "pending" or request.current_position is None:
        return
    step = _step_at(request, request.current_position)
    assert step is not None
    _record(ctx, db, request, step, "skipped", comment=(reason or "").strip()[:4000] or None)
    if not await _activate_from(ctx, db, request, int(step["position"]) + 1):
        _complete(db, request, "approved")
    await db.flush()


async def reassign_current_step(
    ctx: RequestContext,
    db: AsyncSession,
    request_id: uuid.UUID,
    *,
    to_user_id: uuid.UUID | None = None,
    to_role_id: uuid.UUID | None = None,
    reason: str = "escalation",
) -> bool:
    """Epic 32 (FR-170): move the current step of a pending request to another internal approver (a user or
    a role). Changes only ``current_approver_*`` and the snapshot step's approver and records the audit entry
    ``approval.step_reassigned`` -- it writes no decision and never approves, rejects or completes anything.
    Returns False when the request is no longer pending (nothing changed)."""
    if (to_user_id is None) == (to_role_id is None):
        msg = "give exactly one of to_user_id / to_role_id"
        raise ValueError(msg)
    request = (
        await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update())
    ).scalar_one_or_none()
    if request is None:
        raise NotFound("Approval request not found")
    if request.status != "pending" or request.current_position is None:
        return False
    previous = {
        "kind": request.current_approver_kind,
        "user_id": str(request.current_approver_user_id) if request.current_approver_user_id else None,
        "role_id": str(request.current_approver_role_id) if request.current_approver_role_id else None,
    }
    kind = "user" if to_user_id is not None else "role"
    request.current_approver_user_id = to_user_id
    request.current_approver_role_id = to_role_id
    request.current_approver_client_user_id = None
    request.current_approver_kind = kind
    snapshot = [dict(step) for step in request.chain_snapshot]
    for step in snapshot:
        if int(step["position"]) == request.current_position:
            step["resolved_kind"] = kind
            step["approver_user_id"] = str(to_user_id) if to_user_id else None
            step["approver_role_id"] = str(to_role_id) if to_role_id else None
            step["approver_client_user_id"] = None
            step["org"] = None
            step["reassigned"] = {"from": previous, "reason": reason, "at": _now().isoformat()}
    request.chain_snapshot = snapshot
    await db.flush()
    from app.core import audit

    audit.record(
        "approval.step_reassigned",
        "approval_request",
        request.id,
        session=db,
        position=request.current_position,
        reason=reason,
        previous=previous,
        to_user_id=str(to_user_id) if to_user_id else None,
        to_role_id=str(to_role_id) if to_role_id else None,
    )
    await db.flush()
    _ = ctx
    return True


# ================================================================ reads
async def _decisions(db: AsyncSession, request_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[DecisionOut]]:
    out: dict[uuid.UUID, list[DecisionOut]] = {rid: [] for rid in request_ids}
    if not request_ids:
        return out
    rows = await db.execute(
        select(ApprovalDecision)
        .where(ApprovalDecision.request_id.in_(request_ids))
        .order_by(ApprovalDecision.decided_at, ApprovalDecision.position, ApprovalDecision.created_at)
    )
    for d in rows.scalars():
        out[d.request_id].append(DecisionOut.model_validate(d))
    return out


async def _summaries(
    ctx: RequestContext, db: AsyncSession, requests: list[ApprovalRequest]
) -> dict[uuid.UUID, SubjectSummary]:
    by_type: dict[str, list[uuid.UUID]] = {}
    for r in requests:
        by_type.setdefault(r.subject_type, []).append(r.subject_id)
    out: dict[uuid.UUID, SubjectSummary] = {}
    for subject_type, ids in by_type.items():
        definition = subject(subject_type)
        if definition is not None and definition.describe is not None:
            out.update(await definition.describe(ctx, db, ids))
    return out


def _out(
    ctx: RequestContext,
    request: ApprovalRequest,
    decisions: list[DecisionOut],
    summary: SubjectSummary | None,
) -> RequestOut:
    step = _step_at(request, request.current_position)
    principal = ctx.principal if isinstance(ctx.principal, Principal) else None
    facts = (
        dict(request.facts)
        if can_view_compensation(principal)
        else mask_compensation(dict(request.facts), principal)
    )
    return RequestOut(
        id=request.id,
        subject_type=request.subject_type,
        subject_id=request.subject_id,
        subject=summary,
        chain_id=request.chain_id,
        chain_snapshot=request.chain_snapshot,
        facts=facts,
        status=request.status,
        current_position=request.current_position,
        current_step_name=step["name"] if step else None,
        current_approver_user_id=request.current_approver_user_id,
        current_approver_role_id=request.current_approver_role_id,
        current_approver_kind=request.current_approver_kind,
        current_approver_client_user_id=request.current_approver_client_user_id,
        steps_source=request.steps_source,
        submitted_by=request.submitted_by,
        submitted_at=request.submitted_at,
        completed_at=request.completed_at,
        decisions=decisions,
    )


async def _request_out(ctx: RequestContext, db: AsyncSession, request: ApprovalRequest) -> RequestOut:
    await db.refresh(request)
    decisions = await _decisions(db, [request.id])
    summaries = await _summaries(ctx, db, [request])
    return _out(ctx, request, decisions[request.id], summaries.get(request.subject_id))


def _pending_for(principal: Principal) -> Any:
    conds = [ApprovalRequest.current_approver_user_id == principal.id]
    if principal.role_ids:
        conds.append(ApprovalRequest.current_approver_role_id.in_(principal.role_ids))
    return or_(*conds)


_CLIENT_ANY_MATCH = text(
    "EXISTS (SELECT 1 FROM jsonb_array_elements(approval_requests.chain_snapshot) s "
    "WHERE (s->>'position')::int = approval_requests.current_position "
    "AND s->'org'->>1 = :client_id "
    "AND (s->'approver_ref'->>'portal_permission' IS NULL "
    "     OR s->'approver_ref'->>'portal_permission' = ANY(CAST(:perms AS text[]))))"
)


async def _client_pending_condition(ctx: RequestContext, db: AsyncSession, principal: Principal) -> Any:
    any_cond = (ApprovalRequest.current_approver_kind == "client_any") & _CLIENT_ANY_MATCH.bindparams(
        client_id=str(principal.org_id), perms=sorted(principal.portal_permissions)
    )
    from app.core.client_scope import visible_placement_ids

    visible = await visible_placement_ids(ctx, db)
    if visible is not None:
        allowed = [str(v) for v in visible]
        placement = ApprovalRequest.facts["placement_id"].astext
        any_cond = any_cond & (placement.is_(None) | placement.in_(allowed or [""]))
    return or_(ApprovalRequest.current_approver_client_user_id == principal.id, any_cond)


async def list_my_pending(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[RequestOut]:
    principal = ctx.principal
    if isinstance(principal, Principal) and principal.type == "client_user":
        condition = await _client_pending_condition(ctx, db, principal)
    else:
        condition = _pending_for(_principal(ctx))
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == "pending", condition)
        .order_by(ApprovalRequest.submitted_at, ApprovalRequest.id)
    )
    page = await paginate(db, stmt, params)
    requests: list[ApprovalRequest] = list(page.items)
    decisions = await _decisions(db, [r.id for r in requests])
    summaries = await _summaries(ctx, db, requests)
    return Page[RequestOut](
        items=[_out(ctx, r, decisions[r.id], summaries.get(r.subject_id)) for r in requests],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


def _may_view(principal: Principal, request: ApprovalRequest) -> bool:
    permission = _subject_permission(request.subject_type)
    if request.submitted_by == principal.id or (permission is not None and principal.has(permission)):
        return True
    for step in request.chain_snapshot:
        if step.get("approver_user_id") == str(principal.id):
            return True
        role = _uuid(step.get("approver_role_id"))
        if role is not None and role in principal.role_ids:
            return True
    return False


async def get_request(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> RequestOut:
    principal = _principal(ctx)
    request = await db.get(ApprovalRequest, request_id)
    if request is None or not _may_view(principal, request):
        raise NotFound("Approval request not found")
    return await _request_out(ctx, db, request)


async def history_for(
    ctx: RequestContext, db: AsyncSession, subject_type: str, subject_id: uuid.UUID
) -> list[RequestOut]:
    """All requests of one subject, newest first (the caller checks access to the subject)."""
    requests = list(
        (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.subject_type == subject_type, ApprovalRequest.subject_id == subject_id)
                .order_by(ApprovalRequest.submitted_at.desc(), ApprovalRequest.id)
            )
        ).scalars()
    )
    decisions = await _decisions(db, [r.id for r in requests])
    summaries = await _summaries(ctx, db, requests)
    return [_out(ctx, r, decisions[r.id], summaries.get(r.subject_id)) for r in requests]


def registered_subjects() -> list[str]:
    return sorted(subjects())


# ================================================================ logging handlers (Epic 10 notifies)
def _log_step(event: ApprovalStepActivated) -> None:
    logger.info(
        "Approval step activated: request={} subject={}:{} position={} approvers={} role={} kind={}",
        event.request_id,
        event.subject_type,
        event.subject_id,
        event.position,
        len(event.approver_user_ids),
        event.approver_role_id,
        event.approver_kind,
    )


def _log_completed(event: ApprovalCompleted) -> None:
    logger.info(
        "Approval completed: request={} subject={}:{} status={}",
        event.request_id,
        event.subject_type,
        event.subject_id,
        event.status,
    )


subscribe(ApprovalStepActivated, _log_step)
subscribe(ApprovalCompleted, _log_completed)

_ = Decimal  # EscalationSpec amounts are Decimals (re-exported types keep the import)
