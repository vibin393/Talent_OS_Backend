# ruff: noqa: E501
"""Shared helpers of the ``crm`` domain: principal / scope, user refs, status history, money formatting."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import money
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import Principal, check, scope_condition
from app.models.crm import CrmStatusHistory
from app.models.identity import User
from app.schemas.crm import CurrencyTotal, UserRef

LEAD_READ, LEAD_WRITE = "lead:read", "lead:write"
OPP_READ, OPP_WRITE = "opportunity:read", "opportunity:write"


def now() -> datetime:
    return datetime.now(UTC)


def user_principal(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    return p if isinstance(p, Principal) and p.type == "user" else None


def principal_of(ctx: RequestContext, permission: str | None = None) -> Principal:
    """Return the signed-in user (CRM is internal-only: any other principal sees nothing)."""
    p = user_principal(ctx)
    if p is None or p.tenant_id is None:
        raise NotFound("Resource not found")
    if permission is not None:
        check(p, permission)
    return p


def scoped(stmt: Any, principal: Principal | None, permission: str, owner_col: Any) -> Any:
    """``scope_filter`` on ``owner_col`` (system contexts see everything)."""
    if principal is None:
        return stmt
    cond = scope_condition(principal, permission, owner_col)
    return stmt if cond is None else stmt.where(cond)


def actor_kind_of(ctx: RequestContext) -> str:
    if ctx.actor_kind:
        return ctx.actor_kind
    return "user" if user_principal(ctx) is not None else "system"


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


async def user_refs(db: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, UserRef]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await db.execute(select(User.id, User.name).where(User.id.in_(wanted)))
    return {i: UserRef(id=i, name=n or "") for i, n in rows.tuples()}


async def check_user(db: AsyncSession, user_id: uuid.UUID, field: str) -> None:
    active = (await db.execute(select(User.is_active).where(User.id == user_id))).scalar_one_or_none()
    if not active:
        raise ValidationFailed(
            f"Unknown or inactive user for {field}",
            code="invalid_reference",
            details={
                "fields": [
                    {"field": field, "message": "Unknown or inactive user", "type": "invalid_reference"}
                ]
            },
        )


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def weighted(amount: Decimal, pct: Decimal, currency: str) -> Decimal:
    """``estimated_revenue x probability / 100`` rounded ROUND_HALF_UP to the currency's minor units (AD-12)."""
    try:
        return money.percent_of(amount, pct, currency)
    except ValueError:  # unknown currency: 2 decimals
        return money.percent_of(amount, pct, "INR")


def totals(rows: Iterable[tuple[str, Decimal, Decimal]]) -> list[CurrencyTotal]:
    """``(currency, estimated, weighted)`` rows -> per-currency totals (no FX, grouped by currency)."""
    acc: dict[str, list[Decimal]] = {}
    for currency, est, wtd in rows:
        cur = acc.setdefault(currency, [Decimal(0), Decimal(0)])
        cur[0] += est or Decimal(0)
        cur[1] += wtd or Decimal(0)
    return [
        CurrencyTotal(currency=c, estimated=format(v[0], "f"), weighted=format(v[1], "f"))
        for c, v in sorted(acc.items())
    ]


def transition_refused(entity: str, frm: str | None, to: str) -> ConflictError:
    return ConflictError(
        f"A {entity} cannot move from {frm} to {to}",
        code="transition_not_allowed",
        details={"from": frm, "to": to},
    )


def reason_required(
    field: str = "reason_code", message: str = "A reason code is required"
) -> ValidationFailed:
    return ValidationFailed(
        message,
        code="reason_code_required" if field == "reason_code" else "note_required",
        details={"fields": [{"field": field, "message": message, "type": "required"}]},
    )


def add_history(
    db: AsyncSession,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    frm: str | None,
    to: str,
    actor_kind: str,
    actor_id: uuid.UUID | None,
    reason_code: str | None = None,
    note: str | None = None,
    competitor: str | None = None,
) -> None:
    db.add(
        CrmStatusHistory(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            entity_kind=entity_kind,
            entity_id=entity_id,
            from_value=frm,
            to_value=to,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason_code=reason_code,
            note=note,
            competitor=competitor,
            at=now(),
        )
    )


async def history(db: AsyncSession, entity_kind: str, entity_id: uuid.UUID) -> list[CrmStatusHistory]:
    rows = await db.execute(
        select(CrmStatusHistory)
        .where(CrmStatusHistory.entity_kind == entity_kind, CrmStatusHistory.entity_id == entity_id)
        .order_by(CrmStatusHistory.at, CrmStatusHistory.id)
    )
    return list(rows.scalars())
