"""Timesheet configuration (FR-128): tenant settings, Client defaults and versioned Placement configs, plus
the
named approval route ``placement_time_route`` (registered in ``hooks.py``; Expenses and Leave call
``approvals.resolve_route`` and never import ``time``, AD-32).

A config change writes a **new version** effective from the first period not yet submitted; submitted
Timesheets keep their ``config_version`` (AD-24).
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.placements import Placement
from app.models.time import Timesheet, TimesheetConfig, TimesheetConfigDefault
from app.schemas.time import (
    TIME_CATEGORIES,
    ClientTimeDefaultsOut,
    RouteStep,
    TimeConfigIn,
    TimeConfigValues,
    TimeSettingsOut,
    TimeSettingsUpdate,
    TimesheetConfigOut,
)
from app.services.approvals import StepSpec
from app.services.time import calendar
from app.services.time.common import get_placement, invalid, settings_row, tenant_of

DEFAULT_PAY_MULTIPLIERS: dict[str, str] = {
    "regular": "1.0000",
    "ot": "1.5000",
    "double_time": "2.0000",
    "holiday": "1.0000",
    "leave_paid": "1.0000",
    "leave_unpaid": "0.0000",
}
DEFAULT_BILL_MULTIPLIERS: dict[str, str] = {
    "regular": "1.0000",
    "ot": "1.5000",
    "double_time": "2.0000",
    "holiday": "0.0000",
    "leave_paid": "0.0000",
    "leave_unpaid": "0.0000",
}
DEFAULT_ROUTE: list[dict[str, Any]] = [
    {"kind": "placement_account_manager", "ref": None, "label": "Account Manager"},
    {"kind": "client_any", "ref": None, "label": "Client"},
    {"kind": "finance_role", "ref": "finance_admin", "label": "Finance"},
]
CONFIG_FIELDS: tuple[str, ...] = tuple(TimeConfigValues.model_fields)
_MAX_MULTIPLIER = Decimal("9999999999.9999")
_STEP_LABELS = {
    "placement_account_manager": "Account Manager",
    "user": "Approver",
    "client_user": "Client",
    "client_any": "Client",
    "finance_role": "Finance",
}
CLIENT_STEP_KINDS = frozenset({"client_user", "client_any"})


# ---------------------------------------------------------------- settings
def _settings_out(row: Any) -> TimeSettingsOut:
    return TimeSettingsOut(
        missing_grace_days=row.missing_grace_days,
        reminder_offsets=list(row.reminder_offsets or []),
        copy_previous_enabled=row.copy_previous_enabled,
        import_allow_client_approved=row.import_allow_client_approved,
        allow_empty_route=row.allow_empty_route,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> TimeSettingsOut:
    return _settings_out(await settings_row(db, tenant_of(ctx, db)))


async def update_settings(ctx: RequestContext, db: AsyncSession, data: TimeSettingsUpdate) -> TimeSettingsOut:
    row = await settings_row(db, tenant_of(ctx, db))
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        if key == "reminder_offsets" and any(not -30 <= int(v) <= 30 for v in value):
            raise invalid("invalid_settings", "Reminder offsets must be between -30 and 30 days")
        setattr(row, key, value)
    await db.flush()
    return _settings_out(row)


# ---------------------------------------------------------------- values
def default_values() -> dict[str, Any]:
    values = TimeConfigValues().model_dump()
    values["pay_multipliers"] = {k: Decimal(v) for k, v in DEFAULT_PAY_MULTIPLIERS.items()}
    values["bill_multipliers"] = {k: Decimal(v) for k, v in DEFAULT_BILL_MULTIPLIERS.items()}
    values["approval_route"] = [dict(s) for s in DEFAULT_ROUTE]
    return values


def values_of(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in CONFIG_FIELDS:
        value = getattr(row, name)
        if name in ("pay_multipliers", "bill_multipliers"):
            value = {k: Decimal(str(v)) for k, v in (value or {}).items()}
        elif name in ("work_days", "enabled_categories", "approval_route"):
            value = list(value or [])
        out[name] = value
    return out


def _to_columns(values: dict[str, Any]) -> dict[str, Any]:
    cols = {k: values[k] for k in CONFIG_FIELDS}
    cols["pay_multipliers"] = {k: str(v) for k, v in values["pay_multipliers"].items()}
    cols["bill_multipliers"] = {k: str(v) for k, v in values["bill_multipliers"].items()}
    cols["approval_route"] = [
        RouteStep.model_validate(s).model_dump(mode="json") for s in values.get("approval_route") or []
    ]
    cols["enabled_categories"] = list(values["enabled_categories"])
    cols["work_days"] = sorted({int(d) for d in values["work_days"]})
    return cols


def _check_hours(name: str, value: Decimal | None, *, required: bool = False) -> None:
    if value is None:
        if required:
            raise invalid("invalid_config", f"{name} is required", field=name)
        return
    if value < 0 or value > 24 or value != value.quantize(Decimal("0.01")):
        raise invalid("invalid_config", f"{name} must be 0..24 with at most 2 decimals", field=name)


def validate_values(data: TimeConfigIn) -> dict[str, Any]:
    values = data.model_dump()
    for name in ("hours_per_day", "daily_cap", "ot_daily_threshold", "dt_daily_threshold"):
        _check_hours(name, values[name], required=name in ("hours_per_day", "daily_cap"))
    if values["hours_per_day"] <= 0 or values["daily_cap"] <= 0:
        raise invalid("invalid_config", "Hours per day and the daily cap must be positive")
    weekly = values["ot_weekly_threshold"]
    if weekly is not None and (weekly < 0 or weekly != weekly.quantize(Decimal("0.01")) or weekly > 168):
        raise invalid("invalid_config", "The weekly OT threshold must be 0..168", field="ot_weekly_threshold")
    ot, dt = values["ot_daily_threshold"], values["dt_daily_threshold"]
    if ot is not None and dt is not None and dt < ot:
        raise invalid("invalid_config", "The double-time threshold must not be below the OT threshold")
    if "regular" not in values["enabled_categories"]:
        raise invalid("invalid_config", "The regular category is always enabled", field="enabled_categories")
    if not values["work_days"] or any(not 0 <= int(d) <= 6 for d in values["work_days"]):
        raise invalid("invalid_config", "Work days are weekday numbers 0..6", field="work_days")
    for field, defaults in (
        ("pay_multipliers", DEFAULT_PAY_MULTIPLIERS),
        ("bill_multipliers", DEFAULT_BILL_MULTIPLIERS),
    ):
        merged = {k: Decimal(v) for k, v in defaults.items()}
        for key, raw in values[field].items():
            if key not in TIME_CATEGORIES:
                raise invalid("invalid_config", f"Unknown time category {key}", field=field)
            value = Decimal(raw)
            if value < 0 or value > _MAX_MULTIPLIER or value != value.quantize(Decimal("0.0001")):
                raise invalid("invalid_config", "Multipliers are >= 0 with at most 4 decimals", field=field)
            merged[key] = value
        values[field] = merged
    for i, step in enumerate(values["approval_route"] or []):
        if step["kind"] in ("user", "client_user"):
            try:
                uuid.UUID(str(step.get("ref")))
            except ValueError as exc:
                raise invalid("invalid_route", "This step needs a user", step=i) from exc
    return values


# ---------------------------------------------------------------- client defaults
async def _client_default(db: AsyncSession, client_id: uuid.UUID) -> TimesheetConfigDefault | None:
    return (
        await db.execute(select(TimesheetConfigDefault).where(TimesheetConfigDefault.client_id == client_id))
    ).scalar_one_or_none()


async def _assert_client(db: AsyncSession, client_id: uuid.UUID) -> None:
    from app.models.clients import Client

    if (await db.execute(select(Client.id).where(Client.id == client_id))).scalar_one_or_none() is None:
        raise NotFound("Client not found")


async def get_client_defaults(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
) -> ClientTimeDefaultsOut:
    await _assert_client(db, client_id)
    row = await _client_default(db, client_id)
    if row is None:
        return ClientTimeDefaultsOut(client_id=client_id, is_default=True, **default_values())
    return ClientTimeDefaultsOut(
        client_id=client_id, is_default=False, updated_at=row.updated_at, **values_of(row)
    )


async def put_client_defaults(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: TimeConfigIn
) -> ClientTimeDefaultsOut:
    await _assert_client(db, client_id)
    cols = _to_columns(validate_values(data))
    row = await _client_default(db, client_id)
    if row is None:
        row = TimesheetConfigDefault(
            id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), client_id=client_id, **cols
        )
        db.add(row)
    else:
        for key, value in cols.items():
            setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return ClientTimeDefaultsOut(
        client_id=client_id, is_default=False, updated_at=row.updated_at, **values_of(row)
    )


# ---------------------------------------------------------------- placement configs
def _anchor(p: Placement) -> date:
    return p.actual_start_date or p.planned_start_date


async def latest_config(db: AsyncSession, placement_id: uuid.UUID) -> TimesheetConfig | None:
    return (
        await db.execute(
            select(TimesheetConfig)
            .where(TimesheetConfig.placement_id == placement_id)
            .order_by(TimesheetConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if getattr(principal, "type", None) == "user" else None


async def ensure_config(ctx: RequestContext, db: AsyncSession, placement: Placement) -> TimesheetConfig:
    """Version 1 from the Client default (else the tenant default); idempotent."""
    existing = await latest_config(db, placement.id)
    if existing is not None:
        return existing
    row_default = await _client_default(db, placement.client_id)
    values = values_of(row_default) if row_default is not None else default_values()
    start, _ = calendar.period_bounds(values["period_kind"], values["week_start"], _anchor(placement))
    cfg = TimesheetConfig(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        placement_id=placement.id,
        version=1,
        effective_period_start=start,
        created_by=_user_id(ctx),
        **_to_columns(values),
    )
    try:
        async with db.begin_nested():
            db.add(cfg)
            await db.flush()
    except IntegrityError:  # concurrent creation: the other row wins
        existing = await latest_config(db, placement.id)
        if existing is None:
            raise
        return existing
    await db.refresh(cfg)
    return cfg


async def config_version(db: AsyncSession, placement_id: uuid.UUID, version: int) -> TimesheetConfig:
    row = (
        await db.execute(
            select(TimesheetConfig).where(
                TimesheetConfig.placement_id == placement_id, TimesheetConfig.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = await latest_config(db, placement_id)
    if row is None:
        msg = f"placement {placement_id} has no timesheet config"
        raise LookupError(msg)
    return row


async def config_on(
    ctx: RequestContext, db: AsyncSession, placement: Placement, period_start: date
) -> TimesheetConfig:
    """Return the version effective for a period starting on ``period_start``."""
    await ensure_config(ctx, db, placement)
    row = (
        await db.execute(
            select(TimesheetConfig)
            .where(
                TimesheetConfig.placement_id == placement.id,
                TimesheetConfig.effective_period_start <= period_start,
            )
            .order_by(TimesheetConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row if row is not None else await config_version(db, placement.id, 1)


def config_out(row: TimesheetConfig) -> TimesheetConfigOut:
    return TimesheetConfigOut(
        id=row.id,
        placement_id=row.placement_id,
        version=row.version,
        effective_period_start=row.effective_period_start,
        created_at=row.created_at,
        **values_of(row),
    )


async def get_config(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> TimesheetConfigOut:
    placement = await get_placement(db, placement_id)
    return config_out(await ensure_config(ctx, db, placement))


async def config_history(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> list[TimesheetConfigOut]:
    placement = await get_placement(db, placement_id)
    await ensure_config(ctx, db, placement)
    rows = await db.execute(
        select(TimesheetConfig)
        .where(TimesheetConfig.placement_id == placement_id)
        .order_by(TimesheetConfig.version)
    )
    return [config_out(r) for r in rows.scalars()]


async def update_config(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: TimeConfigIn
) -> TimesheetConfigOut:
    placement = await get_placement(db, placement_id)
    current = await ensure_config(ctx, db, placement)
    values = validate_values(data)
    last_submitted = (
        await db.execute(
            select(func.max(Timesheet.period_end)).where(
                Timesheet.placement_id == placement_id,
                Timesheet.kind == "regular",
                Timesheet.status != "draft",
            )
        )
    ).scalar_one_or_none()
    if last_submitted is None:
        effective = current.effective_period_start
    else:
        effective = last_submitted + timedelta(days=1)
    row = TimesheetConfig(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        placement_id=placement_id,
        version=current.version + 1,
        effective_period_start=effective,
        created_by=_user_id(ctx),
        **_to_columns(values),
    )
    db.add(row)
    await db.flush()
    # unsubmitted (draft) timesheets from the effective period on follow the new version
    await db.execute(
        update(Timesheet)
        .where(
            Timesheet.placement_id == placement_id,
            Timesheet.status == "draft",
            Timesheet.period_start >= effective,
        )
        .values(config_version=row.version)
        .execution_options(synchronize_session=False)
    )
    await db.refresh(row)
    return config_out(row)


# ---------------------------------------------------------------- named approval route (R5)
def route_steps(placement: Placement, route: list[dict[str, Any]], *, include_client: bool) -> list[StepSpec]:
    steps: list[StepSpec] = []
    for raw in route:
        kind = str(raw.get("kind"))
        ref = raw.get("ref")
        name = str(raw.get("label") or _STEP_LABELS.get(kind, kind))[:100]
        if kind in CLIENT_STEP_KINDS and not include_client:
            continue
        if kind == "placement_account_manager":
            if placement.account_manager_user_id is None:
                continue
            steps.append(StepSpec(name, "placement_account_manager", {}))
        elif kind == "user":
            steps.append(StepSpec(name, "user", {"user_id": str(ref)}))
        elif kind == "client_user":
            steps.append(StepSpec(name, "client_user", {"client_user_id": str(ref)}))
        elif kind == "client_any":
            steps.append(
                StepSpec(
                    name,
                    "client_any",
                    {"client_id": str(placement.client_id), "portal_permission": "time:approve"},
                )
            )
        elif kind == "finance_role":
            steps.append(StepSpec(name, "finance_role", {"role_key": str(ref or "finance_admin")}))
    return steps


async def placement_time_route(
    ctx: RequestContext, db: AsyncSession, *, placement_id: uuid.UUID, include_client: bool = True
) -> list[StepSpec]:
    """Build the steps of a Placement's timesheet route (reused by Expenses / Leave through
    ``resolve_route``). Client steps
    are dropped when ``include_client`` is false; an Account Manager step is dropped when the placement has
    none."""
    placement = await get_placement(db, uuid.UUID(str(placement_id)))
    cfg = await ensure_config(ctx, db, placement)
    return route_steps(placement, list(cfg.approval_route or []), include_client=include_client)
