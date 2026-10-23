# ruff: file-ignore[unused-function-argument, line-too-long]
"""Generic (Phase 2) SLA targets and the milestone catalogue (Epic 32, story 19.1, FR-168).

Targets live in the Phase 1 ``sla_targets`` table (foundation R14); rows with ``subject_type='demand'`` stay
owned by the Phase 1 editor (``/settings/sla-targets``) and are refused here (422 ``use_demand_sla_settings``).
A target's ``scope`` keys must be among the milestone's registered ``scope_keys`` (plus ``priority``); the most
specific active target wins at clock start (foundation ``start_clock``).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.sla import SlaTarget
from app.schemas.sla import GenericSlaTargetIn, GenericSlaTargetOut, GenericSlaTargetUpdate, SlaMilestoneOut
from app.services.sla.registry import milestone as milestone_def
from app.services.sla.registry import milestones

# FR-168 binding keys (subject_type, milestone) -> (label, owner epic, scope keys). The owning epic registers
# each one from its ``hooks.py`` (``register_sla_milestone``) and starts / stops the clocks.
MILESTONE_CATALOGUE: dict[tuple[str, str], tuple[str, str, tuple[str, ...]]] = {
    ("vendor_submission", "review"): ("Vendor submission review", "17", ()),
    ("vendor_release", "first_submission"): ("First vendor submission after release", "17", ("vendor_id",)),
    ("bgv", "check_turnaround"): ("BGV check turnaround", "20", ("bgv_package_id",)),
    ("onboarding_case", "completion"): ("Onboarding completion before start", "23", ()),
    ("timesheet", "step_decision"): ("Timesheet approval step decision", "25", ("route_step",)),
    ("invoice", "issue_after_period"): ("Invoice issued after billing period", "29", ()),
    ("dispute", "resolution"): ("Dispute resolution", "30", ()),
}


def list_milestones() -> list[SlaMilestoneOut]:
    registered = milestones()
    keys = sorted(set(registered) | set(MILESTONE_CATALOGUE))
    out: list[SlaMilestoneOut] = []
    for key in keys:
        definition = registered.get(key)
        label, owner, scope_keys = MILESTONE_CATALOGUE.get(
            key, (key[1].replace("_", " ").capitalize(), None, ())
        )
        out.append(
            SlaMilestoneOut(
                subject_type=key[0],
                milestone=key[1],
                label=definition.label if definition else label,
                scope_keys=list(definition.scope_keys if definition else scope_keys),
                calendar=definition.calendar if definition else "business",
                owner_epic=owner,
                available=definition is not None,
            )
        )
    return out


def _out(row: SlaTarget) -> GenericSlaTargetOut:
    scope = dict(row.scope or {})
    return GenericSlaTargetOut(
        id=row.id,
        subject_type=row.subject_type,
        milestone=row.milestone,
        scope={k: v for k, v in scope.items() if k != "priority"},
        priority=scope.get("priority"),
        target_hours=row.target_hours,
        at_risk_percent=row.at_risk_percent,
        calendar=row.calendar,
        is_active=row.is_active,
        available=milestone_def(row.subject_type, row.milestone) is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _invalid(reason: str, message: str, path: str | None = None) -> ValidationFailed:
    details: dict[str, Any] = {"reason": reason}
    if path:
        details["path"] = path
    return ValidationFailed(message, code="invalid_sla_target", details=details)


def _scope(
    subject_type: str, milestone: str, scope: Mapping[str, str], priority: str | None
) -> dict[str, str]:
    if subject_type == "demand":
        raise ValidationFailed(
            "Demand SLA targets are edited in the demand SLA settings", code="use_demand_sla_settings"
        )
    definition = milestone_def(subject_type, milestone)
    if definition is None:
        raise _invalid("unknown_milestone", f"Unknown SLA milestone {subject_type}.{milestone}", "milestone")
    allowed = set(definition.scope_keys)
    for key, value in scope.items():
        if key not in allowed:
            raise _invalid(
                "scope_mismatch", f"Scope '{key}' does not apply to this milestone", f"scope.{key}"
            )
        if not str(value).strip():
            raise _invalid("scope_mismatch", f"Scope '{key}' needs a value", f"scope.{key}")
    out = {k: str(v) for k, v in scope.items()}
    if priority is not None:
        out["priority"] = priority
    return out


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


async def list_targets(
    ctx: RequestContext,
    db: AsyncSession,
    subject_type: str | None = None,
) -> list[GenericSlaTargetOut]:
    stmt = select(SlaTarget).where(SlaTarget.subject_type != "demand")
    if subject_type:
        stmt = stmt.where(SlaTarget.subject_type == subject_type)
    rows = (
        await db.execute(stmt.order_by(SlaTarget.subject_type, SlaTarget.milestone, SlaTarget.created_at))
    ).scalars()
    return [_out(r) for r in rows]


async def _save(db: AsyncSession, row: SlaTarget) -> None:
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "A target for this milestone and scope already exists", code="sla_target_exists"
        ) from exc


async def create_target(
    ctx: RequestContext, db: AsyncSession, data: GenericSlaTargetIn
) -> GenericSlaTargetOut:
    scope = _scope(data.subject_type, data.milestone, data.scope, data.priority)
    row = SlaTarget(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        subject_type=data.subject_type,
        milestone=data.milestone,
        priority=None,
        scope=scope,
        target_hours=data.target_hours,
        clock=data.calendar,
        calendar=data.calendar,
        at_risk_percent=data.at_risk_percent,
        is_active=data.is_active,
    )
    db.add(row)
    await _save(db, row)
    await db.refresh(row)
    return _out(row)


async def _get(db: AsyncSession, target_id: uuid.UUID) -> SlaTarget:
    row = await db.get(SlaTarget, target_id)
    if row is None or row.subject_type == "demand":
        raise NotFound("SLA target not found")
    return row


async def update_target(
    ctx: RequestContext,
    db: AsyncSession,
    target_id: uuid.UUID,
    data: GenericSlaTargetUpdate,
) -> GenericSlaTargetOut:
    row = await _get(db, target_id)
    sent = data.model_fields_set
    current = dict(row.scope or {})
    if {"scope", "priority"} & sent:
        scope = (
            data.scope
            if "scope" in sent and data.scope is not None
            else {k: v for k, v in current.items() if k != "priority"}
        )
        priority = data.priority if "priority" in sent else current.get("priority")
        row.scope = _scope(row.subject_type, row.milestone, scope, priority)
    if data.target_hours is not None:
        row.target_hours = data.target_hours
    if data.at_risk_percent is not None:
        row.at_risk_percent = data.at_risk_percent
    if data.calendar is not None:
        row.calendar = row.clock = data.calendar
    if data.is_active is not None:
        row.is_active = data.is_active
    await _save(db, row)
    await db.refresh(row)
    return _out(row)


async def delete_target(ctx: RequestContext, db: AsyncSession, target_id: uuid.UUID) -> None:
    row = await _get(db, target_id)
    await db.delete(row)
    await db.flush()
