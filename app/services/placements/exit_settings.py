# ruff: file-ignore[line-too-long]
"""Epic 31 shared helpers: exit settings, offboarding templates (default seeded per tenant on first use), the
end-date history writer and owner resolution by role."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.placements import Placement
from app.models.placements_exit import OffboardingTemplate, PlacementEndDateHistory, PlacementExitSettings
from app.schemas.placements_exit import (
    ExitSettingsOut,
    ExitSettingsUpdate,
    TemplateIn,
    TemplateOut,
    TemplateUpdate,
)
from app.services.placements.common import tenant_of

DEFAULT_ITEMS: list[dict[str, Any]] = [
    {
        "kind": "client_confirmation",
        "label": "Client confirms last working day",
        "mandatory": True,
        "owner_role": "account_manager",
        "due_offset_days": -5,
        "verifier": None,
    },
    {
        "kind": "final_timesheet_approved",
        "label": "Final timesheet approved",
        "mandatory": True,
        "owner_role": "account_manager",
        "due_offset_days": 3,
        "verifier": "final_timesheet_approved",
    },
    {
        "kind": "final_expense_settlement",
        "label": "Final expenses settled",
        "mandatory": True,
        "owner_role": "finance",
        "due_offset_days": 7,
        "verifier": "final_expense_settlement",
    },
    {
        "kind": "final_payroll_inclusion",
        "label": "Included in final payroll",
        "mandatory": True,
        "owner_role": "payroll",
        "due_offset_days": 10,
        "verifier": "final_payroll_inclusion",
    },
    {
        "kind": "final_invoice",
        "label": "Final invoice issued",
        "mandatory": True,
        "owner_role": "finance",
        "due_offset_days": 10,
        "verifier": "final_invoice",
    },
    {
        "kind": "equipment_return",
        "label": "Equipment returned",
        "mandatory": False,
        "owner_role": "account_manager",
        "due_offset_days": 0,
        "verifier": None,
    },
    {
        "kind": "access_removal",
        "label": "Access removed",
        "mandatory": True,
        "owner_role": "account_manager",
        "due_offset_days": 1,
        "verifier": None,
    },
    {
        "kind": "relieving_letter",
        "label": "Relieving letter issued",
        "mandatory": False,
        "owner_role": "hr",
        "due_offset_days": 3,
        "verifier": None,
    },
    {
        "kind": "experience_letter",
        "label": "Experience letter issued",
        "mandatory": False,
        "owner_role": "hr",
        "due_offset_days": 7,
        "verifier": None,
    },
    {
        "kind": "exit_feedback",
        "label": "Exit feedback recorded",
        "mandatory": False,
        "owner_role": "recruiter",
        "due_offset_days": 3,
        "verifier": None,
    },
]
ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "hr": ("hr_onboarding", "hr", "hr_admin"),
    "finance": ("finance", "finance_admin"),
    "payroll": ("payroll", "finance_admin", "finance"),
}


# ---------------------------------------------------------------- settings
async def settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> PlacementExitSettings:
    row = await db.get(PlacementExitSettings, tenant_id)
    if row is None:
        row = PlacementExitSettings(
            tenant_id=tenant_id,
            alert_offsets_days=[60, 30, 15],
            notify_client_users=False,
            bench_excluded_reasons=["converted", "misconduct", "worker_resignation"],
            early_attrition_days=30,
            auto_case_grace_days=0,
        )
        db.add(row)
        await db.flush()
    return row


async def get_settings(ctx: RequestContext, db: AsyncSession) -> ExitSettingsOut:
    return ExitSettingsOut.model_validate(await settings_row(db, tenant_of(ctx, db)))


async def update_settings(ctx: RequestContext, db: AsyncSession, data: ExitSettingsUpdate) -> ExitSettingsOut:
    row = await settings_row(db, tenant_of(ctx, db))
    values = data.model_dump(exclude_unset=True, exclude_none=True)
    if "alert_offsets_days" in values:
        values["alert_offsets_days"] = sorted(set(values["alert_offsets_days"]), reverse=True)
    for key, value in values.items():
        setattr(row, key, value)
    await db.flush()
    return ExitSettingsOut.model_validate(row)


# ---------------------------------------------------------------- templates
async def default_template(db: AsyncSession, tenant_id: uuid.UUID) -> OffboardingTemplate:
    row = (
        await db.execute(select(OffboardingTemplate).where(OffboardingTemplate.is_default.is_(True)))
    ).scalar_one_or_none()
    if row is None:
        row = OffboardingTemplate(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="Standard offboarding",
            is_default=True,
            items=DEFAULT_ITEMS,
        )
        db.add(row)
        await db.flush()
    return row


async def get_template(db: AsyncSession, template_id: uuid.UUID) -> OffboardingTemplate:
    row = await db.get(OffboardingTemplate, template_id)
    if row is None:
        raise NotFound("Offboarding template not found")
    return row


async def list_templates(ctx: RequestContext, db: AsyncSession) -> list[TemplateOut]:
    await default_template(db, tenant_of(ctx, db))
    rows = (await db.execute(select(OffboardingTemplate).order_by(OffboardingTemplate.created_at))).scalars()
    return [TemplateOut.model_validate(r) for r in rows]


def _check_items(items: list[Any]) -> list[dict[str, Any]]:
    kinds = [i.kind for i in items]
    if len(set(kinds)) != len(kinds):
        raise ValidationFailed("Template item kinds must be unique", code="duplicate_item_kind")
    return [i.model_dump() for i in items]


async def _clear_default(db: AsyncSession, keep: uuid.UUID | None) -> None:
    stmt = update(OffboardingTemplate).where(OffboardingTemplate.is_default.is_(True))
    if keep is not None:
        stmt = stmt.where(OffboardingTemplate.id != keep)
    await db.execute(stmt.values(is_default=False))


async def create_template(ctx: RequestContext, db: AsyncSession, data: TemplateIn) -> TemplateOut:
    tenant_id = tenant_of(ctx, db)
    await default_template(db, tenant_id)
    if data.is_default:
        await _clear_default(db, None)
    row = OffboardingTemplate(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=data.name,
        is_default=data.is_default,
        items=_check_items(data.items),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return TemplateOut.model_validate(row)


async def update_template(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    template_id: uuid.UUID,
    data: TemplateUpdate,
) -> TemplateOut:
    row = await get_template(db, template_id)
    if data.name is not None:
        row.name = data.name
    if data.items is not None:
        row.items = _check_items(data.items)
    if data.is_default:
        await _clear_default(db, row.id)
        await db.flush()
        row.is_default = True
    await db.flush()
    await db.refresh(row)
    return TemplateOut.model_validate(row)


# ---------------------------------------------------------------- end date + owners
async def set_end_date(
    db: AsyncSession,
    placement: Placement,
    new: date | None,
    cause: str,
    ref_id: uuid.UUID | None,
    actor: ActorRef,
) -> None:
    old = placement.end_date
    placement.end_date = new
    db.add(
        PlacementEndDateHistory(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            old_end_date=old,
            new_end_date=new,
            cause=cause,
            ref_id=ref_id,
            actor_kind=actor.kind,
            actor_id=actor.id,
        )
    )
    await db.flush()


async def _role_user(db: AsyncSession, keys: tuple[str, ...]) -> uuid.UUID | None:
    from app.models.identity import Role, User, UserRole

    for key in keys:
        uid = (
            await db.execute(
                select(UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .join(User, User.id == UserRole.user_id)
                .where(Role.key == key, User.is_active.is_(True))
                .order_by(UserRole.user_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if uid is not None:
            return uid
    return None


async def resolve_owners(
    db: AsyncSession, placement: Placement, roles: set[str]
) -> dict[str, uuid.UUID | None]:
    """Owner per role, resolved now: AM / recruiter from the placement; HR / finance / payroll by role key with
    the account manager (then recruiter, then a Tenant Admin) as fallback."""
    fallback = placement.account_manager_user_id or placement.recruiter_user_id
    if fallback is None:
        fallback = await _role_user(db, ("tenant_admin",))
    out: dict[str, uuid.UUID | None] = {}
    for role in roles:
        if role == "account_manager":
            out[role] = placement.account_manager_user_id or fallback
        elif role == "recruiter":
            out[role] = placement.recruiter_user_id or fallback
        else:
            out[role] = await _role_user(db, ROLE_KEYS.get(role, ())) or fallback
    return out
