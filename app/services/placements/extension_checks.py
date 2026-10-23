# ruff: file-ignore[line-too-long]
"""Extension checks (FR-163, PRD §5.15): ``run(ctx, db, placement, proposal) -> list[CheckResult]``.

Checks: ``rate_card`` (commercials ``validate_rate`` with ``subject_kind='extension'``), ``margin`` (tenant
minimum), ``credentials`` (``compliance_blocked`` cache and failed ``credential`` gate states), ``po_validity``
(the PO covers ``[current_end + 1, new_end]``), ``po_balance`` (estimated value fits the PO), ``contract_end``
(warning: the Contract ends before the new end date) and ``offboarding_in_progress`` (an open case fails).

Upper-domain facts never enter here (AD-32): commercials and compliance are read through their facades /
registered gate resolvers only.
"""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.placements import Placement
from app.models.placements_exit import CASE_OPEN, OffboardingCase
from app.schemas.placements_exit import CheckResult
from app.services.placements import margin

# Fails that an approval (extension approval step or exception approval) may override.
EXCEPTION_CHECKS = frozenset({"credentials", "po_validity", "po_balance"})
APPROVAL_CHECKS = frozenset({"rate_card", "margin", "po_validity", "po_balance"})
HOURS_PER_DAY = Decimal(8)
MONTH_DAYS = Decimal(22)


@dataclasses.dataclass(frozen=True)
class Proposal:
    current_end_date: date
    new_end_date: date
    segment_effective_from: date | None = None
    rate_unit: str | None = None
    pay_rate: Decimal | None = None
    bill_rate: Decimal | None = None
    vendor_rate: Decimal | None = None
    burden_pct: Decimal | None = None
    po_id: uuid.UUID | None = None


def working_days(start: date, end: date) -> int:
    """Weekdays in ``[start, end]``."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full, rest = divmod(days, 7)
    count = full * 5
    for i in range(rest):
        if (start + timedelta(days=full * 7 + i)).weekday() < 5:
            count += 1
    return count


def estimate_amount(bill_rate: Decimal | None, unit: str | None, start: date, end: date) -> Decimal | None:
    if bill_rate is None:
        return None
    wd = Decimal(working_days(start, end))
    if unit == "hour":
        return bill_rate * wd * HOURS_PER_DAY
    if unit == "day":
        return bill_rate * wd
    if unit == "month":
        return (bill_rate * wd / MONTH_DAYS).quantize(Decimal("0.01"))
    return bill_rate


async def _current_segment(db: AsyncSession, placement_id: uuid.UUID) -> Any:
    from app.models.placements import RateSegment

    return (
        await db.execute(
            select(RateSegment)
            .where(RateSegment.placement_id == placement_id, RateSegment.status == "active")
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _r(name: str, status: str, overridable: bool = False, **details: Any) -> CheckResult:
    return CheckResult(name=name, status=status, overridable=overridable, details=details)


async def _rate_card(
    ctx: RequestContext, db: AsyncSession, p: Placement, prop: Proposal, unit: str
) -> CheckResult:
    if prop.bill_rate is None:
        return _r("rate_card", "pass", skipped=True)
    from app.schemas.commercials import RateCheckRequest
    from app.services import commercials_service

    try:
        async with db.begin_nested():
            result = await commercials_service.validate_rate(
                ctx,
                db,
                RateCheckRequest(
                    subject_kind="extension",
                    subject_id=None,
                    client_id=p.client_id,
                    contract_id=p.contract_id,
                    on_date=prop.segment_effective_from or prop.current_end_date + timedelta(days=1),
                    role=p.role_title,
                    location=p.work_location_text,
                    bill_rate=prop.bill_rate,
                    vendor_rate=prop.vendor_rate,
                    currency=p.currency,
                    rate_unit=unit,
                ),
            )
    except Exception:  # commercials unavailable / misconfigured: never blocks, surfaces a warning
        logger.exception("extension rate check failed for placement {}", p.id)
        return _r("rate_card", "warn", unavailable=True)
    if result.requires_exception:
        return _r(
            "rate_card",
            "fail",
            True,
            rate_status=result.status,
            min_bill_rate=str(result.min_bill_rate or ""),
        )
    return _r("rate_card", "pass", rate_status=result.status)


async def _margin(
    db: AsyncSession, p: Placement, prop: Proposal
) -> tuple[CheckResult, Decimal | None, Decimal | None]:
    from app.services.placements.common import settings_row

    if prop.bill_rate is None and prop.pay_rate is None and prop.vendor_rate is None:
        return _r("margin", "pass", skipped=True), None, None
    amount, pct = margin.compute(
        bill_rate=prop.bill_rate,
        pay_rate=prop.pay_rate,
        burden_pct=prop.burden_pct,
        vendor_rate=prop.vendor_rate,
        currency=p.currency,
    )
    minimum = (await settings_row(db, p.tenant_id)).min_margin_pct
    if pct is not None and minimum is not None and pct < Decimal(minimum):
        return _r("margin", "fail", True, minimum_pct=str(minimum)), amount, pct
    return _r("margin", "pass"), amount, pct


async def _credentials(ctx: RequestContext, db: AsyncSession, p: Placement, prop: Proposal) -> CheckResult:
    from app.services.placements.registry import start_gate_kinds

    failed: list[str] = []
    definition = start_gate_kinds().get("credential")
    if definition is not None and definition.resolver is not None:
        try:
            async with db.begin_nested():
                states = await definition.resolver(ctx, db, p.id)
            for s in states:
                expires = (s.detail or {}).get("expires_on") or (s.detail or {}).get("valid_to")
                expiring = expires is not None and str(expires) < prop.new_end_date.isoformat()
                if s.required and (s.status == "failed" or expiring):
                    failed.append(s.source_ref)
        except Exception:
            logger.exception("credential gate resolver failed for placement {}", p.id)
    if p.compliance_blocked or failed:
        return _r("credentials", "fail", True, compliance_blocked=bool(p.compliance_blocked), failed=failed)
    return _r("credentials", "pass")


async def _po(
    ctx: RequestContext, db: AsyncSession, p: Placement, prop: Proposal, unit: str, bill: Decimal | None
) -> list[CheckResult]:
    po_id = prop.po_id or p.po_id
    if po_id is None:
        return [_r("po_validity", "pass", po_required=False), _r("po_balance", "pass", skipped=True)]
    from app.services import commercials_service

    start = prop.current_end_date + timedelta(days=1)
    est = estimate_amount(bill, unit, start, prop.new_end_date)
    try:
        validity = await commercials_service.validate_po(ctx, db, po_id, start, period_end=prop.new_end_date)
        balance = (
            await commercials_service.validate_po(
                ctx, db, po_id, start, amount=est, period_end=prop.new_end_date
            )
            if est is not None and validity.valid
            else None
        )
    except Exception:
        logger.exception("PO validation failed for placement {}", p.id)
        return [_r("po_validity", "warn", unavailable=True), _r("po_balance", "warn", unavailable=True)]
    out = [
        _r("po_validity", "pass", po_id=str(po_id))
        if validity.valid
        else _r("po_validity", "fail", True, po_id=str(po_id), reason=validity.reason)
    ]
    if balance is None:
        out.append(_r("po_balance", "pass", skipped=True, est_amount=str(est) if est is not None else None))
    elif balance.valid:
        out.append(_r("po_balance", "pass", est_amount=str(est)))
    else:
        out.append(
            _r(
                "po_balance",
                "fail",
                True,
                est_amount=str(est),
                reason=balance.reason,
                remaining_amount=str(balance.remaining_amount)
                if balance.remaining_amount is not None
                else None,
            )
        )
    return out


async def _contract_end(ctx: RequestContext, db: AsyncSession, p: Placement, prop: Proposal) -> CheckResult:
    if p.contract_id is None:
        return _r("contract_end", "pass", contract=None)
    from app.services import commercials_service

    try:
        contract = await commercials_service.get_contract(ctx, db, p.contract_id)
    except Exception:
        logger.exception("contract read failed for placement {}", p.id)
        return _r("contract_end", "warn", unavailable=True)
    if contract.end_date is not None and contract.end_date < prop.new_end_date:
        return _r(
            "contract_end", "warn", contract_id=str(contract.id), contract_end=contract.end_date.isoformat()
        )
    return _r("contract_end", "pass")


async def open_case(db: AsyncSession, placement_id: uuid.UUID) -> OffboardingCase | None:
    return (
        await db.execute(
            select(OffboardingCase).where(
                OffboardingCase.placement_id == placement_id, OffboardingCase.status.in_(CASE_OPEN)
            )
        )
    ).scalar_one_or_none()


async def run(
    ctx: RequestContext, db: AsyncSession, placement: Placement, prop: Proposal
) -> tuple[list[CheckResult], Decimal | None, Decimal | None]:
    """Run every check; return ``(checks, margin_amount, margin_pct)``."""
    seg = await _current_segment(db, placement.id)
    unit = prop.rate_unit or (seg.rate_unit if seg is not None else "hour")
    bill = prop.bill_rate if prop.bill_rate is not None else (seg.bill_rate if seg is not None else None)
    checks = [await _rate_card(ctx, db, placement, prop, unit)]
    m, amount, pct = await _margin(db, placement, prop)
    checks.append(m)
    checks.append(await _credentials(ctx, db, placement, prop))
    checks.extend(await _po(ctx, db, placement, prop, unit, bill))
    checks.append(await _contract_end(ctx, db, placement, prop))
    case = await open_case(db, placement.id)
    checks.append(
        _r("offboarding_in_progress", "fail", case_id=str(case.id))
        if case is not None
        else _r("offboarding_in_progress", "pass")
    )
    return checks, amount, pct


def failing(checks: list[CheckResult] | list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for c in checks:
        name, status = (c["name"], c["status"]) if isinstance(c, dict) else (c.name, c.status)
        if status == "fail":
            out.add(name)
    return out
