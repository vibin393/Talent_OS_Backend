"""Leave Types and Client overrides (Epic 26, FR-135). ``billable`` defaults to false; ``timesheet_category``
derives from ``paid``. A referenced type is archived instead of deleted. ``effective_rule`` merges the
Placement's Client override over the type."""

import dataclasses
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.time import (
    LeaveBalanceEntry,
    LeaveClientSettings,
    LeaveRequest,
    LeaveType,
    LeaveTypeClientOverride,
)
from app.schemas.leave import (
    LeaveClientSettingsIn,
    LeaveClientSettingsOut,
    LeaveTypeIn,
    LeaveTypeOut,
    LeaveTypeOverrideIn,
    LeaveTypeOverrideOut,
    LeaveTypeUpdate,
)
from app.services.time.accruals import AccrualRule
from app.services.time.common import now, tenant_of

_DAY_FIELDS = ("accrual_amount", "cap", "carry_forward_limit", "negative_limit")


@dataclasses.dataclass(frozen=True, slots=True)
class EffectiveRule:
    leave_type_id: uuid.UUID
    code: str
    name: str
    paid: bool
    billable: bool
    accrual_mode: str
    accrual_amount: Decimal
    cap: Decimal | None
    carry_forward_limit: Decimal | None
    allow_negative: bool
    negative_limit: Decimal | None
    allow_half_day: bool
    requires_client_approval: bool
    timesheet_category: str

    @property
    def accrual(self) -> AccrualRule:
        return AccrualRule(self.accrual_amount, self.cap)


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _override_out(o: LeaveTypeClientOverride) -> LeaveTypeOverrideOut:
    return LeaveTypeOverrideOut(
        id=o.id,
        leave_type_id=o.leave_type_id,
        client_id=o.client_id,
        accrual_amount=o.accrual_amount,
        cap=o.cap,
        billable=o.billable,
        requires_client_approval=o.requires_client_approval,
    )


def _out(t: LeaveType, overrides: list[LeaveTypeClientOverride] | None = None) -> LeaveTypeOut:
    return LeaveTypeOut(
        id=t.id,
        code=t.code,
        name=t.name,
        paid=t.paid,
        billable=t.billable,
        accrual_mode=t.accrual_mode,
        accrual_amount=t.accrual_amount,
        cap=t.cap,
        carry_forward_limit=t.carry_forward_limit,
        allow_negative=t.allow_negative,
        negative_limit=t.negative_limit,
        allow_half_day=t.allow_half_day,
        eligible_worker_types=list(t.eligible_worker_types or []),
        eligible_legal_entity_ids=list(t.eligible_legal_entity_ids or []),
        timesheet_category=t.timesheet_category,
        archived_at=t.archived_at,
        overrides=[_override_out(o) for o in overrides or []],
    )


async def get_type(db: AsyncSession, leave_type_id: uuid.UUID) -> LeaveType:
    t = (await db.execute(select(LeaveType).where(LeaveType.id == leave_type_id))).scalar_one_or_none()
    if t is None:
        raise NotFound("Leave type not found")
    return t


async def _overrides(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[LeaveTypeClientOverride]]:
    out: dict[uuid.UUID, list[LeaveTypeClientOverride]] = {}
    if not ids:
        return out
    for o in (
        await db.execute(
            select(LeaveTypeClientOverride).where(LeaveTypeClientOverride.leave_type_id.in_(ids))
        )
    ).scalars():
        out.setdefault(o.leave_type_id, []).append(o)
    return out


async def list_types(
    ctx: RequestContext, db: AsyncSession, *, include_archived: bool = False
) -> list[LeaveTypeOut]:
    del ctx
    stmt = select(LeaveType).order_by(LeaveType.name)
    if not include_archived:
        stmt = stmt.where(LeaveType.archived_at.is_(None))
    rows = list((await db.execute(stmt)).scalars())
    ovs = await _overrides(db, [t.id for t in rows])
    return [_out(t, ovs.get(t.id)) for t in rows]


async def get_type_out(ctx: RequestContext, db: AsyncSession, leave_type_id: uuid.UUID) -> LeaveTypeOut:
    del ctx
    t = await get_type(db, leave_type_id)
    return _out(t, (await _overrides(db, [t.id])).get(t.id))


def _validate(values: dict[str, Any]) -> None:
    if (
        values.get("accrual_mode") in ("monthly", "annual_grant")
        and Decimal(values.get("accrual_amount") or 0) <= 0
    ):
        raise ValidationFailed(
            "Set the accrual amount for an accruing leave type", code="accrual_amount_required"
        )


async def create_type(ctx: RequestContext, db: AsyncSession, data: LeaveTypeIn) -> LeaveTypeOut:
    values = data.model_dump()
    _validate(values)
    exists = (await db.execute(select(LeaveType.id).where(LeaveType.code == data.code))).scalar_one_or_none()
    if exists is not None:
        raise ConflictError("A leave type with this code already exists", code="leave_type_code_taken")
    for f in _DAY_FIELDS:
        values[f] = _dec(values[f])
    if values["accrual_amount"] is None:
        values["accrual_amount"] = Decimal("0")
    values["timesheet_category"] = "leave_paid" if data.paid else "leave_unpaid"
    t = LeaveType(id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), **values)
    db.add(t)
    await db.flush()
    return _out(t)


async def update_type(
    ctx: RequestContext, db: AsyncSession, leave_type_id: uuid.UUID, data: LeaveTypeUpdate
) -> LeaveTypeOut:
    t = await get_type(db, leave_type_id)
    changes = data.model_dump(exclude_unset=True, exclude={"archived", "clear"})
    for key, value in changes.items():
        if value is None:
            continue
        setattr(t, key, _dec(value) if key in _DAY_FIELDS else value)
    for key in data.clear:
        setattr(t, key, None)
    _validate({"accrual_mode": t.accrual_mode, "accrual_amount": t.accrual_amount})
    t.timesheet_category = "leave_paid" if t.paid else "leave_unpaid"
    if data.archived is True and t.archived_at is None:
        t.archived_at = now()
    elif data.archived is False:
        t.archived_at = None
    await db.flush()
    return await get_type_out(ctx, db, t.id)


async def delete_type(ctx: RequestContext, db: AsyncSession, leave_type_id: uuid.UUID) -> dict[str, str]:
    del ctx
    t = await get_type(db, leave_type_id)
    used = (
        await db.execute(select(LeaveBalanceEntry.id).where(LeaveBalanceEntry.leave_type_id == t.id).limit(1))
    ).scalar_one_or_none() or (
        await db.execute(select(LeaveRequest.id).where(LeaveRequest.leave_type_id == t.id).limit(1))
    ).scalar_one_or_none()
    if used is not None:
        if t.archived_at is None:
            t.archived_at = now()
        await db.flush()
        return {"status": "archived"}
    from app.models.time import LeaveBalance

    for b in (await db.execute(select(LeaveBalance).where(LeaveBalance.leave_type_id == t.id))).scalars():
        await db.delete(b)
    await db.delete(t)
    await db.flush()
    return {"status": "deleted"}


async def put_override(
    ctx: RequestContext,
    db: AsyncSession,
    leave_type_id: uuid.UUID,
    client_id: uuid.UUID,
    data: LeaveTypeOverrideIn,
) -> LeaveTypeOverrideOut:
    t = await get_type(db, leave_type_id)
    from app.models.clients import Client

    if (await db.execute(select(Client.id).where(Client.id == client_id))).scalar_one_or_none() is None:
        raise NotFound("Client not found")
    row = (
        await db.execute(
            select(LeaveTypeClientOverride).where(
                LeaveTypeClientOverride.leave_type_id == t.id, LeaveTypeClientOverride.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = LeaveTypeClientOverride(
            id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), leave_type_id=t.id, client_id=client_id
        )
        db.add(row)
    row.accrual_amount = _dec(data.accrual_amount)
    row.cap = _dec(data.cap)
    row.billable = data.billable
    row.requires_client_approval = data.requires_client_approval
    await db.flush()
    return _override_out(row)


async def delete_override(
    ctx: RequestContext, db: AsyncSession, leave_type_id: uuid.UUID, client_id: uuid.UUID
) -> None:
    del ctx
    row = (
        await db.execute(
            select(LeaveTypeClientOverride).where(
                LeaveTypeClientOverride.leave_type_id == leave_type_id,
                LeaveTypeClientOverride.client_id == client_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Override not found")
    await db.delete(row)
    await db.flush()


# ---------------------------------------------------------------- Client leave settings
async def client_settings_row(db: AsyncSession, client_id: uuid.UUID) -> LeaveClientSettings | None:
    return (
        await db.execute(select(LeaveClientSettings).where(LeaveClientSettings.client_id == client_id))
    ).scalar_one_or_none()


async def client_mode(db: AsyncSession, client_id: uuid.UUID) -> str:
    row = await client_settings_row(db, client_id)
    return row.leave_client_mode if row is not None else "none"


async def team_calendar_enabled(db: AsyncSession, tenant_id: uuid.UUID, client_id: uuid.UUID) -> bool:
    from app.services.time.holidays import leave_settings_row

    row = await client_settings_row(db, client_id)
    if row is not None and row.client_team_calendar is not None:
        return bool(row.client_team_calendar)
    return bool((await leave_settings_row(db, tenant_id)).client_team_calendar_default)


async def get_client_settings(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID
) -> LeaveClientSettingsOut:
    tenant_id = tenant_of(ctx, db)
    row = await client_settings_row(db, client_id)
    return LeaveClientSettingsOut(
        client_id=client_id,
        leave_client_mode=(row.leave_client_mode if row is not None else "none"),
        client_team_calendar=row.client_team_calendar if row is not None else None,
        team_calendar_enabled=await team_calendar_enabled(db, tenant_id, client_id),
    )


async def put_client_settings(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: LeaveClientSettingsIn
) -> LeaveClientSettingsOut:
    from app.models.clients import Client

    if (await db.execute(select(Client.id).where(Client.id == client_id))).scalar_one_or_none() is None:
        raise NotFound("Client not found")
    row = await client_settings_row(db, client_id)
    if row is None:
        row = LeaveClientSettings(id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), client_id=client_id)
        db.add(row)
    row.leave_client_mode = data.leave_client_mode
    row.client_team_calendar = data.client_team_calendar
    await db.flush()
    return await get_client_settings(ctx, db, client_id)


# ---------------------------------------------------------------- effective rule
async def effective_rule(
    db: AsyncSession, leave_type: LeaveType, client_id: uuid.UUID | None
) -> EffectiveRule:
    ov = None
    if client_id is not None:
        ov = (
            await db.execute(
                select(LeaveTypeClientOverride).where(
                    LeaveTypeClientOverride.leave_type_id == leave_type.id,
                    LeaveTypeClientOverride.client_id == client_id,
                )
            )
        ).scalar_one_or_none()
    requires_client = (
        bool(ov.requires_client_approval)
        if ov is not None and ov.requires_client_approval is not None
        else None
    )
    if requires_client is None and client_id is not None:
        requires_client = (await client_mode(db, client_id)) == "approve"
    return EffectiveRule(
        leave_type_id=leave_type.id,
        code=leave_type.code,
        name=leave_type.name,
        paid=leave_type.paid,
        billable=ov.billable if ov is not None and ov.billable is not None else leave_type.billable,
        accrual_mode=leave_type.accrual_mode,
        accrual_amount=ov.accrual_amount
        if ov is not None and ov.accrual_amount is not None
        else leave_type.accrual_amount,
        cap=ov.cap if ov is not None and ov.cap is not None else leave_type.cap,
        carry_forward_limit=leave_type.carry_forward_limit,
        allow_negative=leave_type.allow_negative,
        negative_limit=leave_type.negative_limit,
        allow_half_day=leave_type.allow_half_day,
        requires_client_approval=bool(requires_client),
        timesheet_category=leave_type.timesheet_category,
    )


def eligible(leave_type: LeaveType, worker_type: str | None, legal_entity_id: uuid.UUID) -> bool:
    if leave_type.archived_at is not None:
        return False
    types = list(leave_type.eligible_worker_types or [])
    if types and (worker_type or "") not in types:
        return False
    les = list(leave_type.eligible_legal_entity_ids or [])
    return not les or legal_entity_id in les
