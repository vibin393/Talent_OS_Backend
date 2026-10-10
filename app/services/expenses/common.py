"""Shared helpers of the ``expenses`` domain: errors, settings row, lookups."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.expenses import ExpenseClaim, ExpenseLine, ExpenseSettings
from app.models.placements import Placement

SOURCE_KIND = "expense_line"
SUBJECT = "expense_claim"
ROUTE = "placement_time_route"
CLIENT_KINDS = ("client_user", "client_any")
EDITABLE = ("draft", "rejected")
IN_FLIGHT = ("submitted", "in_approval")
DECIDED = ("approved", "partially_approved")


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    from app.core.fiscal import today_for

    return today_for("Asia/Kolkata")


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details)


def claim_not_found() -> NotFound:
    return NotFound("Expense claim not found")


def not_editable(status: str) -> ConflictError:
    return ConflictError(
        "This expense claim can no longer be edited", code="claim_not_editable", details={"status": status}
    )


async def settings_row(db: AsyncSession) -> ExpenseSettings:
    tenant_id = tenant_of(db)
    row = await db.get(ExpenseSettings, tenant_id)
    if row is None:
        row = ExpenseSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def get_placement(db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if p is None:
        raise NotFound("Placement not found")
    return p


async def get_claim(db: AsyncSession, claim_id: uuid.UUID, *, lock: bool = False) -> ExpenseClaim:
    stmt = select(ExpenseClaim).where(ExpenseClaim.id == claim_id)
    if lock:
        stmt = stmt.with_for_update()
    claim = (await db.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise claim_not_found()
    return claim


async def claim_lines(db: AsyncSession, claim_id: uuid.UUID) -> list[ExpenseLine]:
    return list(
        (
            await db.execute(
                select(ExpenseLine)
                .where(ExpenseLine.claim_id == claim_id)
                .order_by(ExpenseLine.expense_date, ExpenseLine.created_at, ExpenseLine.id)
            )
        ).scalars()
    )
