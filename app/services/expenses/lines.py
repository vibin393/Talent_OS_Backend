"""Expense lines (FR-138): amount computation (mileage, FX), policy evaluation and flags.

``compute`` builds the line values from a ``LineIn`` against the policy rule effective on the expense date:
mileage = km x rate (the entered amount is ignored), a foreign currency is converted with the tenant FX table
(``fx_rate_missing`` when no rate within the look-back), rounded once per line. ``evaluate_flags`` recomputes
``over_daily_limit`` (same category, same date, every non-rejected claim of the Worker on the Placement),
``over_claim_limit`` (same category in this claim), ``receipt_missing`` and ``possible_duplicate_receipt``
(same receipt hash on another line of the Worker within the window); ``category_disabled`` blocks submit.
"""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.expenses import ExpenseClaim, ExpenseLine
from app.models.files import StoredFile
from app.schemas.expenses import LineIn
from app.services.expenses import amounts
from app.services.expenses.common import invalid, now, settings_row, today
from app.services.expenses.policies import Rule, rule_for

RECEIPT_OWNER = "expense_receipt"


@dataclasses.dataclass(slots=True)
class Computed:
    amount: Decimal
    currency: str
    km: Decimal | None
    fx_rate: Decimal | None
    fx_source: str | None
    fx_rate_date: date | None
    amount_claim_ccy: Decimal
    billable: bool
    rule: Rule
    receipt_sha256: str | None


async def _fx(
    ctx: RequestContext, db: AsyncSession, from_ccy: str, to_ccy: str, on: date
) -> tuple[Decimal, str, date]:
    from app.services import legal_entities_service

    rate = await legal_entities_service.rate_for(ctx, db, from_ccy, to_ccy, on)
    return Decimal(rate.rate), rate.source, rate.rate_date


def placement_window(placement: object) -> tuple[date | None, date]:
    start = getattr(placement, "actual_start_date", None) or getattr(placement, "planned_start_date", None)
    end = getattr(placement, "end_date", None)
    last = today() if end is None else min(end, today())
    return start, last


async def _receipt(db: AsyncSession, claim: ExpenseClaim, file_id: uuid.UUID | None) -> str | None:
    if file_id is None:
        return None
    row = (await db.execute(select(StoredFile).where(StoredFile.id == file_id))).scalar_one_or_none()
    if (
        row is None
        or row.owner_entity_type != RECEIPT_OWNER
        or row.owner_entity_id != claim.candidate_id
        or row.scan_status == "infected"
    ):
        raise invalid("receipt_invalid", "Upload the receipt again", field="receipt_file_id")
    return row.sha256


async def compute(
    ctx: RequestContext, db: AsyncSession, claim: ExpenseClaim, placement: object, data: LineIn
) -> Computed:
    start, last = placement_window(placement)
    if (start is not None and data.expense_date < start) or data.expense_date > last:
        raise invalid(
            "expense_date_outside_placement",
            "The expense date must fall within the placement dates and not in the future",
            start=start.isoformat() if start else None,
            end=last.isoformat(),
        )
    rule = await rule_for(db, claim.client_id, data.category, data.expense_date)
    claim_ccy = claim.currency
    km: Decimal | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    fx_date: date | None = None
    if data.category == "mileage":
        if data.km is None:
            raise invalid("km_required", "Enter the distance in km", field="km")
        km = amounts.parse_km(data.km)
        if rule.mileage_rate_per_km is None:
            raise invalid("mileage_rate_missing", "No mileage rate is set in the expense policy")
        currency = claim_ccy
        amount = amounts.mileage_amount(km, rule.mileage_rate_per_km, claim_ccy)
        in_claim = amount
    else:
        if data.amount is None:
            raise invalid("amount_required", "Enter the amount", field="amount")
        currency = amounts.check_currency(data.currency or claim_ccy)
        amount = amounts.parse_amount(data.amount, currency)
        if currency != claim_ccy:
            fx_rate, fx_source, fx_date = await _fx(ctx, db, currency, claim_ccy, data.expense_date)
        in_claim = amounts.convert(amount, fx_rate, claim_ccy)
    billable = rule.default_billable if data.billable is None else bool(data.billable)
    if rule.rebill_method == "not_billable":
        billable = False
    return Computed(
        amount=amount,
        currency=currency,
        km=km,
        fx_rate=fx_rate,
        fx_source=fx_source,
        fx_rate_date=fx_date,
        amount_claim_ccy=in_claim,
        billable=billable,
        rule=rule,
        receipt_sha256=await _receipt(db, claim, data.receipt_file_id),
    )


async def _limit_in(
    ctx: RequestContext, db: AsyncSession, rule: Rule, limit: Decimal | None, ccy: str, on: date
) -> Decimal | None:
    if limit is None or rule.limit_currency in (None, ccy):
        return limit
    try:
        rate, _src, _d = await _fx(ctx, db, str(rule.limit_currency), ccy, on)
    except ValidationFailed:
        return limit
    return amounts.convert(limit, rate, ccy)


async def _day_total(
    db: AsyncSession, claim: ExpenseClaim, category: str, on: date, exclude: uuid.UUID | None
) -> Decimal:
    stmt = (
        select(func.coalesce(func.sum(ExpenseLine.amount_claim_ccy), 0))
        .join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
        .where(
            ExpenseClaim.candidate_id == claim.candidate_id,
            ExpenseClaim.placement_id == claim.placement_id,
            ExpenseClaim.status != "rejected",
            ExpenseLine.line_status != "rejected",
            ExpenseLine.category == category,
            ExpenseLine.expense_date == on,
        )
    )
    if exclude is not None:
        stmt = stmt.where(ExpenseLine.id != exclude)
    return Decimal((await db.execute(stmt)).scalar_one())


async def _claim_total(
    db: AsyncSession, claim: ExpenseClaim, category: str, exclude: uuid.UUID | None
) -> Decimal:
    stmt = select(func.coalesce(func.sum(ExpenseLine.amount_claim_ccy), 0)).where(
        ExpenseLine.claim_id == claim.id,
        ExpenseLine.category == category,
        ExpenseLine.line_status != "rejected",
    )
    if exclude is not None:
        stmt = stmt.where(ExpenseLine.id != exclude)
    return Decimal((await db.execute(stmt)).scalar_one())


async def _duplicate(
    db: AsyncSession, claim: ExpenseClaim, sha: str | None, exclude: uuid.UUID | None
) -> bool:
    if not sha:
        return False
    window = (await settings_row(db)).duplicate_receipt_window_days
    stmt = (
        select(ExpenseLine.id)
        .join(ExpenseClaim, ExpenseClaim.id == ExpenseLine.claim_id)
        .where(
            ExpenseClaim.candidate_id == claim.candidate_id,
            ExpenseLine.receipt_sha256 == sha,
            ExpenseLine.created_at >= now() - timedelta(days=window),
        )
        .limit(1)
    )
    if exclude is not None:
        stmt = stmt.where(ExpenseLine.id != exclude)
    return (await db.execute(stmt)).first() is not None


async def evaluate_flags(
    ctx: RequestContext,
    db: AsyncSession,
    claim: ExpenseClaim,
    *,
    category: str,
    expense_date: date,
    amount_claim_ccy: Decimal,
    receipt_file_id: uuid.UUID | None,
    receipt_sha256: str | None,
    rule: Rule,
    line_id: uuid.UUID | None,
) -> list[str]:
    flags: list[str] = []
    if not rule.enabled:
        flags.append("category_disabled")
    ccy = claim.currency
    daily = await _limit_in(ctx, db, rule, rule.daily_limit, ccy, expense_date)
    if daily is not None:
        day = await _day_total(db, claim, category, expense_date, line_id)
        if amounts.over_limit(day + amount_claim_ccy, daily):
            flags.append("over_daily_limit")
    per_claim = await _limit_in(ctx, db, rule, rule.claim_limit, ccy, expense_date)
    if per_claim is not None:
        in_claim = await _claim_total(db, claim, category, line_id)
        if amounts.over_limit(in_claim + amount_claim_ccy, per_claim):
            flags.append("over_claim_limit")
    if receipt_file_id is None and amounts.receipt_required(amount_claim_ccy, rule.receipt_required_above):
        flags.append("receipt_missing")
    if await _duplicate(db, claim, receipt_sha256, line_id):
        flags.append("possible_duplicate_receipt")
    return flags


def needs_justification(flags: list[str]) -> bool:
    return any(f != "category_disabled" for f in flags)
