"""Expense policies (FR-137): versioned per scope (tenant, or one Client override); a change writes a new
version effective from a chosen date (default tomorrow). A line is evaluated against the version effective on
its expense date; the effective rule of a category is the Client version's row when present, else the tenant
version's row (category-level merge).
"""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.expenses import EXPENSE_CATEGORIES, ExpensePolicy, ExpensePolicyCategory, ExpenseSettings
from app.schemas.expenses import (
    ExpenseSettingsOut,
    ExpenseSettingsUpdate,
    PolicyCategoryIn,
    PolicyCategoryOut,
    PolicyIn,
    PolicyOut,
    PolicyVersionIn,
    PolicyVersionRef,
)
from app.services.expenses.amounts import check_currency
from app.services.expenses.common import now, settings_row, today


# ================================================================ settings
def _settings_out(row: ExpenseSettings) -> ExpenseSettingsOut:
    return ExpenseSettingsOut(
        ai_receipt_read_enabled=row.ai_receipt_read_enabled,
        duplicate_receipt_window_days=row.duplicate_receipt_window_days,
        max_receipt_mb=row.max_receipt_mb,
        allowed_receipt_types=list(row.allowed_receipt_types or []),
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> ExpenseSettingsOut:  # noqa: ARG001
    return _settings_out(await settings_row(db))


async def update_settings(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    data: ExpenseSettingsUpdate,
) -> ExpenseSettingsOut:
    row = await settings_row(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    return _settings_out(row)


# ================================================================ effective rules
@dataclasses.dataclass(frozen=True, slots=True)
class Rule:
    category: str
    enabled: bool
    daily_limit: Decimal | None
    claim_limit: Decimal | None
    limit_currency: str | None
    receipt_required_above: Decimal | None
    mileage_rate_per_km: Decimal | None
    default_billable: bool
    rebill_method: str
    markup_pct: Decimal | None
    rebill_tax_mode: str
    policy_version_id: uuid.UUID | None


def default_rule(category: str) -> Rule:
    """No policy at all: everything allowed, nothing billable, no mileage rate."""
    return Rule(category, True, None, None, None, None, None, False, "not_billable", None, "taxable", None)


def _rule(row: ExpensePolicyCategory) -> Rule:
    return Rule(
        category=row.category,
        enabled=row.enabled,
        daily_limit=row.daily_limit,
        claim_limit=row.claim_limit,
        limit_currency=row.limit_currency,
        receipt_required_above=row.receipt_required_above,
        mileage_rate_per_km=row.mileage_rate_per_km,
        default_billable=row.default_billable and row.rebill_method != "not_billable",
        rebill_method=row.rebill_method,
        markup_pct=row.markup_pct,
        rebill_tax_mode=row.rebill_tax_mode,
        policy_version_id=row.policy_id,
    )


async def _version_on(
    db: AsyncSession, scope: str, client_id: uuid.UUID | None, on: date
) -> ExpensePolicy | None:
    stmt = select(ExpensePolicy).where(ExpensePolicy.scope == scope, ExpensePolicy.effective_from <= on)
    stmt = (
        stmt.where(ExpensePolicy.client_id == client_id)
        if client_id
        else stmt.where(ExpensePolicy.client_id.is_(None))
    )
    return (
        await db.execute(
            stmt.order_by(ExpensePolicy.effective_from.desc(), ExpensePolicy.version.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def rule_for(db: AsyncSession, client_id: uuid.UUID, category: str, on: date) -> Rule:
    """Return the rule of ``category`` effective on ``on`` (a Client override beats the tenant policy)."""
    for scope, cid in (("client", client_id), ("tenant", None)):
        version = await _version_on(db, scope, cid, on)
        if version is None:
            continue
        row = (
            await db.execute(
                select(ExpensePolicyCategory).where(
                    ExpensePolicyCategory.policy_id == version.id, ExpensePolicyCategory.category == category
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return _rule(row)
    return default_rule(category)


# ================================================================ CRUD
def _decimal(raw: str | None) -> Decimal | None:
    return Decimal(raw) if raw not in (None, "") else None


def _validate_categories(categories: list[PolicyCategoryIn]) -> None:
    seen = [c.category for c in categories]
    if len(seen) != len(set(seen)):
        raise ValidationFailed("Each category can appear once", code="category_duplicate")
    for c in categories:
        check_currency(c.limit_currency)
        if c.rebill_method == "markup" and _decimal(c.markup_pct) is None:
            raise ValidationFailed(
                "Enter the markup %", code="markup_required", details={"category": c.category}
            )
        if c.category == "mileage" and c.enabled and _decimal(c.mileage_rate_per_km) is None:
            raise ValidationFailed(
                "Enter the mileage rate per km",
                code="mileage_rate_required",
                details={"category": c.category},
            )


async def _write_categories(
    db: AsyncSession, tenant_id: uuid.UUID, policy_id: uuid.UUID, categories: list[PolicyCategoryIn]
) -> None:
    for c in categories:
        db.add(
            ExpensePolicyCategory(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                policy_id=policy_id,
                category=c.category,
                enabled=c.enabled,
                daily_limit=_decimal(c.daily_limit),
                claim_limit=_decimal(c.claim_limit),
                limit_currency=c.limit_currency,
                receipt_required_above=_decimal(c.receipt_required_above),
                mileage_rate_per_km=_decimal(c.mileage_rate_per_km),
                default_billable=c.default_billable and c.rebill_method != "not_billable",
                rebill_method=c.rebill_method,
                markup_pct=_decimal(c.markup_pct) if c.rebill_method == "markup" else None,
                rebill_tax_mode=c.rebill_tax_mode,
            )
        )
    await db.flush()


async def _policy_out(db: AsyncSession, policy: ExpensePolicy) -> PolicyOut:
    from app.models.clients import Client

    rows = list(
        (
            await db.execute(
                select(ExpensePolicyCategory).where(ExpensePolicyCategory.policy_id == policy.id)
            )
        ).scalars()
    )
    order = {c: i for i, c in enumerate(EXPENSE_CATEGORIES)}
    rows.sort(key=lambda r: order.get(r.category, 99))
    client_name = None
    if policy.client_id is not None:
        client_name = (
            await db.execute(select(Client.name).where(Client.id == policy.client_id))
        ).scalar_one_or_none()
    versions = await _versions(db, policy.scope, policy.client_id)
    return PolicyOut(
        id=policy.id,
        name=policy.name,
        scope=policy.scope,
        client_id=policy.client_id,
        client_name=client_name,
        version=policy.version,
        effective_from=policy.effective_from,
        superseded_at=policy.superseded_at,
        created_at=policy.created_at,
        categories=[
            PolicyCategoryOut(
                category=r.category,
                enabled=r.enabled,
                daily_limit=r.daily_limit,
                claim_limit=r.claim_limit,
                limit_currency=r.limit_currency,
                receipt_required_above=r.receipt_required_above,
                mileage_rate_per_km=r.mileage_rate_per_km,
                default_billable=r.default_billable,
                rebill_method=r.rebill_method,
                markup_pct=r.markup_pct,
                rebill_tax_mode=r.rebill_tax_mode,
            )
            for r in rows
        ],
        versions=[
            PolicyVersionRef(
                id=v.id, version=v.version, effective_from=v.effective_from, superseded_at=v.superseded_at
            )
            for v in versions
        ],
    )


def _series(stmt: object, scope: str, client_id: uuid.UUID | None) -> object:
    stmt = stmt.where(ExpensePolicy.scope == scope)  # type: ignore[attr-defined]
    if client_id is None:
        return stmt.where(ExpensePolicy.client_id.is_(None))  # type: ignore[attr-defined]
    return stmt.where(ExpensePolicy.client_id == client_id)  # type: ignore[attr-defined]


async def _versions(db: AsyncSession, scope: str, client_id: uuid.UUID | None) -> list[ExpensePolicy]:
    stmt = _series(select(ExpensePolicy), scope, client_id)
    return list((await db.execute(stmt.order_by(ExpensePolicy.version))).scalars())  # type: ignore[attr-defined]


async def _get(db: AsyncSession, policy_id: uuid.UUID) -> ExpensePolicy:
    row = (await db.execute(select(ExpensePolicy).where(ExpensePolicy.id == policy_id))).scalar_one_or_none()
    if row is None:
        raise NotFound("Expense policy not found")
    return row


async def list_policies(ctx: RequestContext, db: AsyncSession) -> list[PolicyOut]:  # noqa: ARG001
    rows = list(
        (
            await db.execute(
                select(ExpensePolicy).order_by(ExpensePolicy.scope.desc(), ExpensePolicy.version.desc())
            )
        ).scalars()
    )
    latest: dict[tuple[str, uuid.UUID | None], ExpensePolicy] = {}
    for r in rows:
        latest.setdefault((r.scope, r.client_id), r)
    out = [await _policy_out(db, p) for p in latest.values()]
    out.sort(key=lambda p: (p.scope != "tenant", p.client_name or "", p.name))
    return out


async def get_policy(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    policy_id: uuid.UUID,
    version: int | None = None,
) -> PolicyOut:
    policy = await _get(db, policy_id)
    if version is not None and version != policy.version:
        match = next(
            (v for v in await _versions(db, policy.scope, policy.client_id) if v.version == version), None
        )
        if match is None:
            raise NotFound("Expense policy version not found")
        policy = match
    return await _policy_out(db, policy)


async def create_policy(ctx: RequestContext, db: AsyncSession, data: PolicyIn) -> PolicyOut:
    from app.models.clients import Client

    if data.scope == "client":
        if data.client_id is None:
            raise ValidationFailed("Choose the Client", code="client_required")
        found = (await db.execute(select(Client.id).where(Client.id == data.client_id))).scalar_one_or_none()
        if found is None:
            raise NotFound("Client not found")
    client_id = data.client_id if data.scope == "client" else None
    if await _versions(db, data.scope, client_id):
        raise ConflictError("A policy already exists for this scope; add a new version", code="policy_exists")
    _validate_categories(data.categories)
    tenant_id = ctx.tenant_id or uuid.UUID(str(db.sync_session.info["tenant_id"]))
    policy = ExpensePolicy(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=data.name,
        scope=data.scope,
        client_id=client_id,
        version=1,
        effective_from=data.effective_from or date(2000, 1, 1),
        created_by=getattr(ctx.principal, "id", None),
    )
    db.add(policy)
    await db.flush()
    await _write_categories(db, tenant_id, policy.id, data.categories)
    await db.refresh(policy)
    return await _policy_out(db, policy)


async def new_version(
    ctx: RequestContext, db: AsyncSession, policy_id: uuid.UUID, data: PolicyVersionIn
) -> PolicyOut:
    base = await _get(db, policy_id)
    versions = await _versions(db, base.scope, base.client_id)
    latest = versions[-1]
    _validate_categories(data.categories)
    effective = data.effective_from or (today() + timedelta(days=1))
    if effective < latest.effective_from:
        raise ValidationFailed(
            "A new version cannot start before the current one",
            code="effective_from_invalid",
            details={"current_effective_from": latest.effective_from.isoformat()},
        )
    tenant_id = latest.tenant_id
    policy = ExpensePolicy(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=data.name or latest.name,
        scope=latest.scope,
        client_id=latest.client_id,
        version=latest.version + 1,
        effective_from=effective,
        created_by=getattr(ctx.principal, "id", None),
    )
    latest.superseded_at = now()
    db.add(policy)
    await db.flush()
    await _write_categories(db, tenant_id, policy.id, data.categories)
    await db.refresh(policy)
    return await _policy_out(db, policy)
