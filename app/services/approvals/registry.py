"""Approval engine registries (Epic 13, AD-24): approval subjects, approver resolvers and delegate resolvers.

Subject domains register from their ``hooks.py``; the engine never imports a subject domain. Callbacks run
synchronously inside the deciding transaction with the deciding ``ActorRef`` unchanged.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.registry import Registry
from app.schemas.approvals import SubjectSummary


@dataclasses.dataclass(frozen=True, slots=True)
class EscalationSpec:
    after_business_hours: Decimal
    action: Literal["notify", "reassign"]
    target_kind: str
    target_ref: uuid.UUID | None = None
    repeat_every_hours: Decimal | None = None
    max_repeats: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class StepSpec:
    name: str
    approver_kind: str
    approver_ref: Mapping[str, Any]
    condition: Mapping[str, Any] | None = None
    escalation: EscalationSpec | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedApprover:
    kind: str  # user | role | client_user | client_any
    user_id: uuid.UUID | None
    role_id: uuid.UUID | None
    client_user_id: uuid.UUID | None
    org: tuple[str, uuid.UUID] | None
    display: str


# (ctx, db, approver_ref, facts)
ApproverResolver = Callable[
    [RequestContext, AsyncSession, Mapping[str, Any], Mapping[str, Any]], Awaitable[ResolvedApprover]
]


@dataclasses.dataclass(frozen=True, slots=True)
class StepDecision:
    request_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    position: int
    step_name: str
    outcome: Literal["approved", "rejected", "changes_requested"]
    actor: ActorRef
    comment: str | None
    is_final: bool
    payload: Mapping[str, Any] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FinalDecision:
    request_id: uuid.UUID | None
    subject_type: str
    subject_id: uuid.UUID
    outcome: Literal["approved", "rejected", "changes_requested", "auto_approved"]
    actor: ActorRef | None
    comment: str | None
    payload: Mapping[str, Any] | None = None


Describe = Callable[[RequestContext, AsyncSession, list[uuid.UUID]], Awaitable[dict[uuid.UUID, SubjectSummary]]]
OnFinalDecided = Callable[[RequestContext, AsyncSession, FinalDecision], Awaitable[None]]
OnStepDecided = Callable[[RequestContext, AsyncSession, StepDecision], Awaitable[None]]
# (ctx, db, deciding principal id, decision date) -> ids the principal may act for
DelegateResolver = Callable[[RequestContext, AsyncSession, uuid.UUID, date], Awaitable[set[uuid.UUID]]]


@dataclasses.dataclass(frozen=True, slots=True)
class SubjectDef:
    kind: str
    on_final_decided: OnFinalDecided
    on_step_decided: OnStepDecided | None
    describe: Describe | None
    permission: str | None
    auto_approve_calls_back: bool


_subjects: Registry[str, SubjectDef] = Registry("approval subject", allow_replace=True)
_resolvers: Registry[str, ApproverResolver] = Registry("approver resolver", allow_replace=True)
_delegates: Registry[str, DelegateResolver] = Registry("delegate resolver", allow_replace=True)


def register_approval_subject(
    kind: str,
    *,
    on_final_decided: OnFinalDecided,
    on_step_decided: OnStepDecided | None = None,
    describe: Describe | None = None,
    permission: str | None = None,
    auto_approve_calls_back: bool = False,
) -> None:
    """Register (or replace -- Phase 1 behaviour, logged at debug) an approval subject type."""
    if not kind or len(kind) > 40:
        msg = f"invalid approval subject type {kind!r}"
        raise ValueError(msg)
    if kind in _subjects:
        logger.debug("Re-registering approval subject {}", kind)
    _subjects.register(
        kind, SubjectDef(kind, on_final_decided, on_step_decided, describe, permission, auto_approve_calls_back)
    )


def unregister_approval_subject(kind: str) -> None:
    _subjects.unregister(kind)


def subject(kind: str) -> SubjectDef | None:
    return _subjects.find(kind)


def subjects() -> Mapping[str, SubjectDef]:
    return _subjects.view()


def register_approver_resolver(kind: str, resolver: ApproverResolver) -> None:
    _resolvers.register(kind, resolver)


def approver_resolver(kind: str) -> ApproverResolver | None:
    return _resolvers.find(kind)


def register_delegate_resolver(principal_kind: str, fn: DelegateResolver) -> None:
    """Epic 18 registers the ``client_user`` resolver (``client_delegations.acting_for``)."""
    _delegates.register(principal_kind, fn)


def delegate_resolver(principal_kind: str) -> DelegateResolver | None:
    return _delegates.find(principal_kind)


def put_subject_definition(definition: SubjectDef) -> None:
    """Restore a previously read ``SubjectDef`` (Phase 1 tests swap subjects and put them back)."""
    _subjects.register(definition.kind, definition)
