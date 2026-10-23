# ruff: file-ignore[unused-function-argument, class-as-data-structure, non-imperative-mood, line-too-long]
"""Escalations (Epic 32, story 19.2, FR-170): policies, watches and the ``sla.evaluate_escalations`` beat.

* **Policies** live in the foundation ``escalation_policies`` table. Reusable ones are ``owner_kind='library'``;
  attaching a library policy to an approval-chain step writes an owned copy (``owner_kind='approval_chain_step'``,
  ``owner_ref='<chain id>:<position>'``, ``source_policy_id`` = the library row). The duration is stored in hours
  (``after_business_hours``); ``after_unit`` is its display unit (1 business day = 8 business hours, applied
  as whole working days).
* **Watches** (``escalation_watches``) are opened when an approval step activates with an escalation (explicit
  steps carry it in the request snapshot; chain steps through their owned copy) and when a workflow
  ``create_task`` action names a policy. ``spec`` snapshots the parameters. They close when the step is decided,
  the request completes / is cancelled, the task completes, or the policy is archived.
* **Firing** can only notify or re-assign: ``approvals.reassign_current_step`` / ``tasks_service.reassign``.
  Nothing here records a decision, approves, rejects or closes anything (FR-170, AST-tested).
"""

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.business_hours import tenant_calendar
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.settings import get_settings
from app.events.sla import EscalationFired
from app.models.approvals import ApprovalChain, ApprovalRequest, EscalationPolicy
from app.models.identity import TeamMember
from app.models.sla import EscalationWatch
from app.models.tasks import Task
from app.schemas.sla import (
    ChainEscalationsIn,
    ChainEscalationsOut,
    ChainStepEscalationOut,
    EscalationPolicyIn,
    EscalationPolicyOut,
    EscalationPolicyUpdate,
    EscalationWatchOut,
)
from app.services import directory_service as directory
from app.services import entity_refs, notifications_service
from app.services.sla import durations

TARGET_TO_DB = {"named_user": "user", "assignee_manager": "manager_of_assignee", "role": "role"}
TARGET_FROM_DB = {v: k for k, v in TARGET_TO_DB.items()}
FALLBACK = "target_unresolved_fallback"


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


# ================================================================ policy <-> hours
def _hours(value: Decimal, unit: str) -> Decimal:
    return value * durations.BUSINESS_DAY_HOURS if unit == "business_days" else value


def _value(hours: Decimal | None, unit: str) -> Decimal | None:
    if hours is None:
        return None
    return hours / durations.BUSINESS_DAY_HOURS if unit == "business_days" else hours


def summary_of(spec: Mapping[str, Any]) -> str:
    unit = str(spec.get("after_unit") or "business_hours")
    after = durations.describe(_value(Decimal(str(spec["after_hours"])), unit) or 0, unit)
    target = {
        "user": "a named user",
        "manager_of_assignee": "the assignee's team lead",
        "role": "a role",
    }.get(str(spec.get("target_kind")), "someone")
    verb = "Reassign to" if spec.get("action") == "reassign" else "Notify"
    text = f"{verb} {target} after {after}"
    repeats = int(spec.get("max_repeats") or 0)
    if repeats and spec.get("repeat_every_hours"):
        every = durations.describe(_value(Decimal(str(spec["repeat_every_hours"])), unit) or 0, unit)
        text += f", repeat every {every} ({repeats}x)"
    return text


def spec_of(policy: EscalationPolicy) -> dict[str, Any]:
    """The parameters a watch snapshots."""
    return {
        "name": policy.name,
        "after_hours": str(policy.after_business_hours),
        "after_unit": policy.after_unit,
        "calendar": policy.calendar,
        "action": policy.action,
        "target_kind": policy.target_kind,
        "target_ref": str(policy.target_ref) if policy.target_ref else None,
        "repeat_every_hours": str(policy.repeat_every_hours)
        if policy.repeat_every_hours is not None
        else None,
        "max_repeats": int(policy.max_repeats),
        "notify_original_assignee": bool(policy.notify_original_assignee),
    }


def spec_from_snapshot(escalation: Mapping[str, Any]) -> dict[str, Any]:
    """An explicit step's ``EscalationSpec`` as the request snapshot stores it (foundation format)."""
    return {
        "name": escalation.get("name"),
        "after_hours": str(escalation["after_business_hours"]),
        "after_unit": escalation.get("after_unit") or "business_hours",
        "calendar": escalation.get("calendar") or "business",
        "action": escalation.get("action") or "notify",
        "target_kind": escalation.get("target_kind") or "manager_of_assignee",
        "target_ref": escalation.get("target_ref"),
        "repeat_every_hours": escalation.get("repeat_every_hours"),
        "max_repeats": int(escalation.get("max_repeats") or 0),
        "notify_original_assignee": bool(escalation.get("notify_original_assignee", True)),
    }


def total_fires(spec: Mapping[str, Any]) -> int:
    repeats = int(spec.get("max_repeats") or 0) if spec.get("repeat_every_hours") else 0
    return 1 + min(repeats, get_settings().escalation_max_repeats)


async def _after(db: AsyncSession, start: datetime, hours: Decimal, spec: Mapping[str, Any]) -> datetime:
    cal, tz = await tenant_calendar(db)
    unit = str(spec.get("after_unit") or "business_hours")
    if unit == "business_days":
        days = max(1, int(hours / durations.BUSINESS_DAY_HOURS))
        return durations.add(start, days, "business_days", cal, tz)
    if unit == "hours" or spec.get("calendar") == "calendar":
        return durations.add(start, hours, "hours", cal, tz)
    return durations.add(start, hours, "business_hours", cal, tz)


# ================================================================ policies (library)
def _policy_out(p: EscalationPolicy) -> EscalationPolicyOut:
    user_ref = p.target_ref if p.target_kind == "user" else None
    role_ref = p.target_ref if p.target_kind == "role" else None
    return EscalationPolicyOut(
        id=p.id,
        name=p.name,
        owner_kind=p.owner_kind,
        owner_ref=p.owner_ref,
        after_value=_value(Decimal(p.after_business_hours), p.after_unit) or Decimal(0),
        after_unit=p.after_unit,
        after_business_hours=p.after_business_hours,
        calendar=p.calendar,
        action=p.action,
        target_kind=TARGET_FROM_DB.get(p.target_kind, "named_user"),
        target_user_id=user_ref,
        target_role_id=role_ref,
        repeat_every_value=_value(p.repeat_every_hours, p.after_unit),
        max_repeats=int(p.max_repeats),
        notify_original_assignee=p.notify_original_assignee,
        is_active=p.is_active,
        archived_at=p.archived_at,
        source_policy_id=p.source_policy_id,
        summary=summary_of(spec_of(p)),
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _invalid(reason: str, path: str, message: str) -> ValidationFailed:
    return ValidationFailed(
        message, code="invalid_escalation_policy", details={"reason": reason, "path": path}
    )


async def _check_targets(
    db: AsyncSession, kind: str, user_id: uuid.UUID | None, role_id: uuid.UUID | None
) -> None:
    if kind == "named_user" and (user_id is None or not await entity_refs.active_user_ids(db, [user_id])):
        raise _invalid("invalid_reference", "target_user_id", "The target user is not an active user")
    if kind == "role" and (role_id is None or not await directory.existing_role_ids(db, [role_id])):
        raise _invalid("invalid_reference", "target_role_id", "The target role does not exist")


def _check_repeats(max_repeats: int) -> None:
    limit = get_settings().escalation_max_repeats
    if max_repeats > limit - 1:
        raise _invalid("too_many_repeats", "max_repeats", f"At most {limit} escalations in total")


async def list_policies(
    ctx: RequestContext,
    db: AsyncSession,
    include_archived: bool = False,
) -> list[EscalationPolicyOut]:
    stmt = select(EscalationPolicy).where(EscalationPolicy.owner_kind == "library")
    if not include_archived:
        stmt = stmt.where(EscalationPolicy.archived_at.is_(None))
    rows = (await db.execute(stmt.order_by(EscalationPolicy.name, EscalationPolicy.id))).scalars()
    return [_policy_out(p) for p in rows]


async def _library(db: AsyncSession, policy_id: uuid.UUID, *, for_update: bool = False) -> EscalationPolicy:
    stmt = select(EscalationPolicy).where(
        EscalationPolicy.id == policy_id, EscalationPolicy.owner_kind == "library"
    )
    if for_update:
        stmt = stmt.with_for_update()
    policy = (await db.execute(stmt)).scalar_one_or_none()
    if policy is None:
        raise NotFound("Escalation policy not found")
    return policy


async def get_policy(ctx: RequestContext, db: AsyncSession, policy_id: uuid.UUID) -> EscalationPolicyOut:
    return _policy_out(await _library(db, policy_id))


async def create_policy(
    ctx: RequestContext, db: AsyncSession, data: EscalationPolicyIn
) -> EscalationPolicyOut:
    await _check_targets(db, data.target_kind, data.target_user_id, data.target_role_id)
    _check_repeats(data.max_repeats)
    policy = EscalationPolicy(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        owner_kind="library",
        owner_ref=None,
        name=data.name,
        after_business_hours=_hours(data.after_value, data.after_unit),
        after_unit=data.after_unit,
        calendar="calendar" if data.after_unit == "hours" else "business",
        action=data.action,
        target_kind=TARGET_TO_DB[data.target_kind],
        target_ref=data.target_user_id or data.target_role_id,
        repeat_every_hours=_hours(data.repeat_every_value, data.after_unit)
        if data.repeat_every_value
        else None,
        max_repeats=data.max_repeats,
        notify_original_assignee=data.notify_original_assignee,
        is_active=data.is_active,
    )
    db.add(policy)
    await db.flush()
    await db.refresh(policy)
    return _policy_out(policy)


async def update_policy(
    ctx: RequestContext, db: AsyncSession, policy_id: uuid.UUID, data: EscalationPolicyUpdate
) -> EscalationPolicyOut:
    policy = await _library(db, policy_id, for_update=True)
    if policy.archived_at is not None:
        raise ConflictError("Archived policies cannot be edited", code="policy_archived")
    current = _policy_out(policy)
    merged = current.model_dump() | data.model_dump(exclude_unset=True)
    if "target_kind" in data.model_fields_set:  # a new target kind replaces both ids
        merged["target_user_id"] = data.target_user_id
        merged["target_role_id"] = data.target_role_id
    try:
        full = EscalationPolicyIn.model_validate(
            {k: merged[k] for k in EscalationPolicyIn.model_fields if k in merged}
        )
    except ValueError as exc:
        raise _invalid("invalid", "body", str(exc).splitlines()[0]) from exc
    await _check_targets(db, full.target_kind, full.target_user_id, full.target_role_id)
    _check_repeats(full.max_repeats)
    policy.name = full.name
    policy.after_business_hours = _hours(full.after_value, full.after_unit)
    policy.after_unit = full.after_unit
    policy.calendar = "calendar" if full.after_unit == "hours" else "business"
    policy.action = full.action
    policy.target_kind = TARGET_TO_DB[full.target_kind]
    policy.target_ref = full.target_user_id or full.target_role_id
    policy.repeat_every_hours = (
        _hours(full.repeat_every_value, full.after_unit) if full.repeat_every_value else None
    )
    policy.max_repeats = full.max_repeats
    policy.notify_original_assignee = full.notify_original_assignee
    policy.is_active = full.is_active
    await db.flush()
    await _refresh_copies(db, policy)
    await db.refresh(policy)
    _ = ctx
    return _policy_out(policy)


async def _refresh_copies(db: AsyncSession, library: EscalationPolicy) -> None:
    """Owned copies follow their library policy (running watches keep their snapshot)."""
    copies = (
        await db.execute(select(EscalationPolicy).where(EscalationPolicy.source_policy_id == library.id))
    ).scalars()
    for copy in copies:
        for attr in (
            "name", "after_business_hours", "after_unit", "calendar", "action", "target_kind", "target_ref",
            "repeat_every_hours", "max_repeats", "notify_original_assignee", "is_active",
        ):  # fmt: skip
            setattr(copy, attr, getattr(library, attr))
    await db.flush()


async def archive_policy(ctx: RequestContext, db: AsyncSession, policy_id: uuid.UUID) -> EscalationPolicyOut:
    """Archive a library policy: its owned copies deactivate and every active watch it (or a copy) started is
    cancelled."""
    policy = await _library(db, policy_id, for_update=True)
    if policy.archived_at is None:
        policy.archived_at, policy.is_active = _now(), False
        copies = list(
            (
                await db.execute(
                    select(EscalationPolicy).where(EscalationPolicy.source_policy_id == policy.id)
                )
            ).scalars()
        )
        for copy in copies:
            copy.is_active = False
        ids = [policy.id, *(c.id for c in copies)]
        await db.execute(
            update(EscalationWatch)
            .where(EscalationWatch.policy_id.in_(ids), EscalationWatch.status == "active")
            .values(status="cancelled", next_fire_at=None, last_outcome="policy_archived", updated_at=_now())
        )
        await db.flush()
    await db.refresh(policy)
    return _policy_out(policy)


async def active_library_policy(db: AsyncSession, policy_id: uuid.UUID) -> EscalationPolicy | None:
    policy = await db.get(EscalationPolicy, policy_id)
    if (
        policy is None
        or policy.owner_kind != "library"
        or policy.archived_at is not None
        or not policy.is_active
    ):
        return None
    return policy


async def policy_names(db: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    rows = await db.execute(
        select(EscalationPolicy.id, EscalationPolicy.name).where(
            EscalationPolicy.owner_kind == "library",
            EscalationPolicy.archived_at.is_(None),
            EscalationPolicy.is_active.is_(True),
        )
    )
    return [(pid, name or "") for pid, name in rows.tuples().all()]


# ================================================================ approval-chain attachments
def _chain_ref(chain_id: uuid.UUID, position: int) -> str:
    return f"{chain_id}:{position}"


async def _chain(db: AsyncSession, chain_id: uuid.UUID) -> ApprovalChain:
    chain = await db.get(ApprovalChain, chain_id)
    if chain is None:
        raise NotFound("Approval chain not found")
    return chain


async def get_chain_escalations(
    ctx: RequestContext, db: AsyncSession, chain_id: uuid.UUID
) -> ChainEscalationsOut:
    await _chain(db, chain_id)
    rows = (
        await db.execute(
            select(EscalationPolicy).where(
                EscalationPolicy.owner_kind == "approval_chain_step",
                EscalationPolicy.owner_ref.like(f"{chain_id}:%"),
            )
        )
    ).scalars()
    steps = []
    for p in rows:
        position = int(str(p.owner_ref).rsplit(":", 1)[1])
        steps.append(
            ChainStepEscalationOut(
                position=position,
                policy_id=p.source_policy_id,
                copy_id=p.id,
                name=p.name,
                summary=summary_of(spec_of(p)),
            )
        )
    return ChainEscalationsOut(chain_id=chain_id, steps=sorted(steps, key=lambda s: s.position))


async def set_chain_escalations(
    ctx: RequestContext, db: AsyncSession, chain_id: uuid.UUID, data: ChainEscalationsIn
) -> ChainEscalationsOut:
    """Replace the escalation of each listed step (``policy_id`` NULL removes it) with an owned copy of the
    library policy. Steps not listed keep theirs."""
    await _chain(db, chain_id)
    for item in data.steps:
        ref = _chain_ref(chain_id, item.position)
        existing = (
            (
                await db.execute(
                    select(EscalationPolicy).where(
                        EscalationPolicy.owner_kind == "approval_chain_step",
                        EscalationPolicy.owner_ref == ref,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            await db.delete(row)
        await db.flush()
        if item.policy_id is None:
            continue
        library = await active_library_policy(db, item.policy_id)
        if library is None:
            raise _invalid(
                "invalid_reference", f"steps[{item.position}].policy_id", "Unknown or archived policy"
            )
        db.add(
            EscalationPolicy(
                id=uuid.uuid4(),
                tenant_id=_tenant(db),
                owner_kind="approval_chain_step",
                owner_ref=ref,
                source_policy_id=library.id,
                name=library.name,
                after_business_hours=library.after_business_hours,
                after_unit=library.after_unit,
                calendar=library.calendar,
                action=library.action,
                target_kind=library.target_kind,
                target_ref=library.target_ref,
                repeat_every_hours=library.repeat_every_hours,
                max_repeats=library.max_repeats,
                notify_original_assignee=library.notify_original_assignee,
                is_active=True,
            )
        )
        await db.flush()
    return await get_chain_escalations(ctx, db, chain_id)


async def _chain_step_policy(db: AsyncSession, chain_id: uuid.UUID, position: int) -> EscalationPolicy | None:
    return (
        await db.execute(
            select(EscalationPolicy).where(
                EscalationPolicy.owner_kind == "approval_chain_step",
                EscalationPolicy.owner_ref == _chain_ref(chain_id, position),
                EscalationPolicy.is_active.is_(True),
                EscalationPolicy.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()


# ================================================================ watches: lifecycle
async def _insert_watch(
    db: AsyncSession,
    *,
    policy_id: uuid.UUID | None,
    spec: Mapping[str, Any],
    subject_kind: str,
    subject_id: uuid.UUID,
    step_position: int | None,
    assignee_ref: Mapping[str, Any],
    started_at: datetime,
) -> uuid.UUID | None:
    first = await _after(db, started_at, Decimal(str(spec["after_hours"])), spec)
    now = _now()
    inserted = (
        await db.execute(
            pg_insert(EscalationWatch)
            .values(
                id=uuid.uuid4(),
                tenant_id=_tenant(db),
                policy_id=policy_id,
                spec=dict(spec),
                subject_kind=subject_kind,
                subject_id=subject_id,
                step_position=step_position,
                assignee_ref=dict(assignee_ref),
                started_at=started_at,
                next_fire_at=first,
                fires=0,
                status="active",
                history=[],
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_escalation_watches_subject_step")
            .returning(EscalationWatch.id)
        )
    ).scalar_one_or_none()
    return inserted


async def close_watches(
    db: AsyncSession,
    subject_kind: str,
    subject_id: uuid.UUID,
    *,
    outcome: str,
    except_position: int | None = None,
    status: str = "done",
) -> int:
    stmt = (
        update(EscalationWatch)
        .where(
            EscalationWatch.subject_kind == subject_kind,
            EscalationWatch.subject_id == subject_id,
            EscalationWatch.status == "active",
        )
        .values(status=status, next_fire_at=None, last_outcome=outcome, updated_at=_now())
    )
    if except_position is not None:
        stmt = stmt.where(EscalationWatch.step_position.is_distinct_from(except_position))
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


async def open_approval_watch(db: AsyncSession, request_id: uuid.UUID, position: int) -> uuid.UUID | None:
    """Called after a step activated (``ApprovalStepActivated``): start the step's escalation, if any."""
    request = await db.get(ApprovalRequest, request_id)
    if request is None or request.status != "pending" or request.current_position != position:
        return None
    await close_watches(db, "approval_step", request.id, outcome="step_advanced", except_position=position)
    step = next((s for s in request.chain_snapshot if int(s["position"]) == position), None)
    if step is None:
        return None
    policy_id: uuid.UUID | None = None
    if step.get("escalation"):
        spec = spec_from_snapshot(step["escalation"])
        policy_id = _uuid(step["escalation"].get("policy_id"))
    elif request.chain_id is not None:
        policy = await _chain_step_policy(db, request.chain_id, position)
        if policy is None:
            return None
        spec, policy_id = spec_of(policy), policy.id
    else:
        return None
    assignee = {
        "kind": request.current_approver_kind,
        "user_id": str(request.current_approver_user_id) if request.current_approver_user_id else None,
        "role_id": str(request.current_approver_role_id) if request.current_approver_role_id else None,
    }
    return await _insert_watch(
        db,
        policy_id=policy_id,
        spec=spec,
        subject_kind="approval_step",
        subject_id=request.id,
        step_position=position,
        assignee_ref=assignee,
        started_at=request.current_step_activated_at or _now(),
    )


async def open_task_watch(db: AsyncSession, task_id: uuid.UUID, policy_id: uuid.UUID) -> uuid.UUID | None:
    """A workflow ``create_task`` with ``escalation_policy_id`` (library policy)."""
    policy = await active_library_policy(db, policy_id)
    task = await db.get(Task, task_id)
    if policy is None or task is None or task.status != "open":
        return None
    return await _insert_watch(
        db,
        policy_id=policy.id,
        spec=spec_of(policy),
        subject_kind="task",
        subject_id=task.id,
        step_position=None,
        assignee_ref={"kind": "user", "user_id": str(task.owner_id)},
        started_at=task.created_at or _now(),
    )


async def list_watches(
    ctx: RequestContext,
    db: AsyncSession,
    subject_kind: str,
    subject_id: uuid.UUID,
) -> list[EscalationWatchOut]:
    rows = (
        await db.execute(
            select(EscalationWatch)
            .where(EscalationWatch.subject_kind == subject_kind, EscalationWatch.subject_id == subject_id)
            .order_by(EscalationWatch.started_at, EscalationWatch.id)
        )
    ).scalars()
    return [
        EscalationWatchOut(
            id=w.id,
            policy_id=w.policy_id,
            subject_kind=w.subject_kind,
            subject_id=w.subject_id,
            step_position=w.step_position,
            started_at=w.started_at,
            next_fire_at=w.next_fire_at,
            fires=w.fires,
            status=w.status,
            last_outcome=w.last_outcome,
            history=list(w.history or []),
            spec=dict(w.spec or {}),
        )
        for w in rows
    ]


# ================================================================ watches: firing
async def _team_leads(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    members = set(user_ids)
    if not members:
        return set()
    team_ids = set(
        (await db.execute(select(TeamMember.team_id).where(TeamMember.user_id.in_(members)))).scalars()
    )
    if not team_ids:
        return set()
    leads = set(
        (
            await db.execute(
                select(TeamMember.user_id).where(
                    TeamMember.team_id.in_(team_ids), TeamMember.is_lead.is_(True)
                )
            )
        ).scalars()
    )
    return await entity_refs.active_user_ids(db, leads - members)


async def _tenant_admins(db: AsyncSession) -> set[uuid.UUID]:
    return await entity_refs.role_user_ids(db, "tenant_admin")


class _Subject:
    """What a watch escalates: the current assignees and the timeline entity."""

    def __init__(
        self,
        assignees: set[uuid.UUID],
        entity: tuple[str, uuid.UUID] | None,
        reassignable: bool,
        request: ApprovalRequest | None = None,
        task: Task | None = None,
    ) -> None:
        self.assignees = assignees
        self.entity = entity
        self.reassignable = reassignable
        self.request = request
        self.task = task


async def _subject(db: AsyncSession, watch: EscalationWatch) -> _Subject | None:
    """The still-pending subject, or None (the watch closes)."""
    if watch.subject_kind == "approval_step":
        request = (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.id == watch.subject_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if request is None or request.status != "pending" or request.current_position != watch.step_position:
            return None
        assignees: set[uuid.UUID] = set()
        if request.current_approver_user_id:
            assignees.add(request.current_approver_user_id)
        if request.current_approver_role_id:
            assignees |= await directory.user_ids_with_role(db, request.current_approver_role_id)
        reassignable = (request.current_approver_kind or "user") in ("user", "role")
        return _Subject(assignees, (request.subject_type, request.subject_id), reassignable, request=request)
    task = (
        await db.execute(
            select(Task).where(Task.id == watch.subject_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None or task.status != "open" or task.archived_at is not None:
        return None
    entity = (task.entity_type, task.entity_id) if task.entity_type and task.entity_id else None
    return _Subject({task.owner_id}, entity, True, task=task)


async def _targets(
    db: AsyncSession, spec: Mapping[str, Any], subject: _Subject
) -> tuple[set[uuid.UUID], bool]:
    """(users to escalate to, resolved?) -- unresolved falls back to the Tenant Admins."""
    kind, ref = spec.get("target_kind"), _uuid(spec.get("target_ref"))
    found: set[uuid.UUID] = set()
    if kind == "user" and ref is not None:
        found = await entity_refs.active_user_ids(db, [ref])
    elif kind == "role" and ref is not None:
        found = await directory.user_ids_with_role(db, ref)
    elif kind == "manager_of_assignee":
        found = await _team_leads(db, subject.assignees)
    if found:
        return found, True
    return await _tenant_admins(db), False


def _first(users: Iterable[uuid.UUID]) -> uuid.UUID | None:
    ordered = sorted(users, key=str)
    return ordered[0] if ordered else None


async def _reassign(
    ctx: RequestContext, db: AsyncSession, spec: Mapping[str, Any], subject: _Subject, targets: set[uuid.UUID]
) -> str:
    from app.services import approvals, tasks_service

    role_id = _uuid(spec.get("target_ref")) if spec.get("target_kind") == "role" else None
    if subject.request is not None:
        if not subject.reassignable:
            return "reassign_not_supported"
        if role_id is not None:
            done = await approvals.reassign_current_step(
                ctx, db, subject.request.id, to_role_id=role_id, reason="escalation"
            )
        else:
            user = _first(targets)
            if user is None:
                return "target_unresolved"
            done = await approvals.reassign_current_step(
                ctx, db, subject.request.id, to_user_id=user, reason="escalation"
            )
        return "reassigned" if done else "subject_closed"
    if subject.task is not None:
        user = _first(targets)
        if user is None:
            return "target_unresolved"
        await tasks_service.reassign(ctx, db, subject.task.id, user)
        return "reassigned"
    return "subject_closed"


def _body(spec: Mapping[str, Any], fire: int, total: int, resolved: bool) -> str:
    unit = str(spec.get("after_unit") or "business_hours")
    hours = Decimal(str(spec["after_hours"]))
    if fire > 1 and spec.get("repeat_every_hours"):
        hours = Decimal(str(spec["after_hours"])) + Decimal(str(spec["repeat_every_hours"])) * (fire - 1)
    after = durations.describe(_value(hours, unit) or 0, unit)
    who = {"user": "a named user", "manager_of_assignee": "the team lead", "role": "a role"}.get(
        str(spec.get("target_kind")), "a user"
    )
    if not resolved:
        who = "the Tenant Admins (no team lead found)"
    verb = "Reassigned" if spec.get("action") == "reassign" and resolved else "Escalated"
    return f"{verb} to {who} after {after} ({fire} of {total})"


async def _fire(ctx: RequestContext, db: AsyncSession, watch: EscalationWatch, now: datetime) -> str:
    spec = dict(watch.spec or {})
    subject = await _subject(db, watch)
    if subject is None:
        watch.status, watch.next_fire_at, watch.last_outcome = "done", None, "subject_closed"
        return "subject_closed"
    fire, total = watch.fires + 1, total_fires(spec)
    targets, resolved = await _targets(db, spec, subject)
    outcome = "notified"
    if spec.get("action") == "reassign" and resolved:
        outcome = await _reassign(ctx, db, spec, subject, targets)
    if not resolved:
        outcome = FALLBACK
    recipients = set(targets)
    if spec.get("notify_original_assignee", True):
        recipients |= subject.assignees
    entity_type, entity_id = subject.entity if subject.entity else (watch.subject_kind, watch.subject_id)
    body = _body(spec, fire, total, resolved)
    if recipients:
        await notifications_service.deliver(
            db,
            "escalation_raised",
            recipients,
            notifications_service.Content(
                title="Approval escalated" if watch.subject_kind == "approval_step" else "Task escalated",
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
                link_path="/approvals" if watch.subject_kind == "approval_step" else "/tasks",
            ),
            event_id=f"esc:{watch.id}:{fire}",
        )
    data = {
        "kind": "escalation",
        "watch_id": str(watch.id),
        "subject_kind": watch.subject_kind,
        "subject_id": str(watch.subject_id),
        "action": spec.get("action"),
        "outcome": outcome,
        "fire": fire,
        "total": total,
        "target_user_ids": sorted(str(u) for u in targets),
    }
    if subject.entity is not None:
        from app.services import activity_service

        await activity_service.add_system_entry(ctx, db, entity_type, entity_id, body, data)
    audit.record(
        "escalation.fired",
        "escalation_watch",
        watch.id,
        session=db,
        escalation_action=data["action"],
        outcome=outcome,
        fire=fire,
        total=total,
        subject_kind=watch.subject_kind,
        subject_id=data["subject_id"],
        target_user_ids=data["target_user_ids"],
    )
    emit(
        db,
        EscalationFired(
            tenant_id=watch.tenant_id,
            watch_id=watch.id,
            policy_id=watch.policy_id,
            subject_kind=watch.subject_kind,
            subject_id=watch.subject_id,
            action=str(spec.get("action")),
            target=str(spec.get("target_kind")),
            fire_number=fire,
        ),
    )
    watch.history = [
        *(watch.history or []),
        {"fire": fire, "at": now.isoformat(), "outcome": outcome, "targets": data["target_user_ids"]},
    ]
    watch.fires, watch.last_outcome = fire, outcome
    if fire < total and spec.get("repeat_every_hours") and watch.next_fire_at is not None:
        watch.next_fire_at = await _after(
            db, watch.next_fire_at, Decimal(str(spec["repeat_every_hours"])), spec
        )
    else:
        watch.status, watch.next_fire_at = "done", None
    return outcome


async def evaluate_escalations(
    db: AsyncSession, now: datetime | None = None, batch: int | None = None
) -> int:
    """Fire every due watch of the session tenant (``FOR UPDATE SKIP LOCKED``); returns the number fired."""
    now = now or _now()
    size = batch or get_settings().workflow_wait_batch
    watches = (
        (
            await db.execute(
                select(EscalationWatch)
                .where(EscalationWatch.status == "active", EscalationWatch.next_fire_at <= now)
                .order_by(EscalationWatch.next_fire_at, EscalationWatch.id)
                .limit(size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    ctx = RequestContext(tenant_id=_tenant(db), actor_kind="system")
    fired = 0
    for watch in watches:
        try:
            async with db.begin_nested():
                outcome = await _fire(ctx, db, watch, now)
                await db.flush()
            if outcome != "subject_closed":
                fired += 1
        except Exception:
            logger.exception("Escalation watch {} failed", watch.id)
    await db.flush()
    return fired
