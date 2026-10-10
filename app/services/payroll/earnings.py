"""Payroll earnings inputs -- pure (AD-20). No I/O, Decimal only, rounding once per item.

Export only (PRD OQ2, binding): nothing here computes statutory deductions or net pay. The row carries
earnings *inputs*; the Tenant's payroll system computes the rest.

* ``hour`` rate: ``quantize(quantity x rate x multiplier)``.
* ``day`` rate: quantity is hours -> ``quantize(quantity x rate x multiplier / hours_per_day)`` (division kept
  unrounded inside the product; quantized once).
* ``month`` rate: regular / holiday / leave items carry no amount; one monthly base item per segment span
  ``quantize(rate x proration_factor)`` (proration from ``time_service.pay_inputs``); OT / DT use the
  unrounded ``hourly_equivalent`` x multiplier.
* ``rate_adjustment``: ``quantize(quantity x rate_delta x multiplier)`` (signed).
* ``expense_line``: the approved claim-currency amount (already rounded).
* one-off kinds (``ONE_OFF_KINDS``, Epic 42 ``referral_reward``): the item's fixed amount, bucketed as an
  adjustment (``adjustments_total`` / gross earnings inputs; no new export column).
Row totals are sums of already-rounded item amounts (a row never re-rounds a sum).
"""

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.core.money import MONEY_CONTEXT, quantize_amount, quantize_hours

ZERO = Decimal(0)
HOUR_CATEGORIES = ("regular", "ot", "double_time", "holiday", "leave_paid", "leave_unpaid")
EARNING_CATEGORIES = ("regular", "ot", "double_time", "holiday", "leave_paid")
MONTHLY_UNPRICED = frozenset({"regular", "holiday", "leave_paid", "leave_unpaid"})
EXPENSE_KINDS = frozenset({"expense_line"})
ADJUSTMENT_KIND = "adjustment_entry"
RATE_ADJUSTMENT_KIND = "rate_adjustment"
MONTHLY_BASE_KIND = "monthly_base"
# Epic 42 (coordinator decision 8): fixed-amount one-off payable items, priced at the item's amount (no pay rate)
# and reported in the adjustments column.
ONE_OFF_KINDS = frozenset({"referral_reward"})

# Statutory placeholder columns (file mapping only, always empty).
PLACEHOLDERS = (
    "pf_employee",
    "pf_employer",
    "esi_employee",
    "esi_employer",
    "professional_tax",
    "tds_salary",
    "other_deductions",
)


def bucket_for(source_kind: str) -> str:
    if source_kind in EXPENSE_KINDS:
        return "reimbursement"
    if source_kind == ADJUSTMENT_KIND or source_kind in ONE_OFF_KINDS:
        return "adjustment"
    if source_kind == RATE_ADJUSTMENT_KIND:
        return "rate_adjustment"
    if source_kind == MONTHLY_BASE_KIND:
        return "base"
    return "earning"


def _mul(*values: Decimal) -> Decimal:
    out = Decimal(1)
    for v in values:
        out = MONEY_CONTEXT.multiply(out, Decimal(v))
    return out


def price_time(
    *,
    quantity: Decimal,
    rate: Decimal,
    rate_unit: str,
    multiplier: Decimal,
    category: str | None,
    currency: str,
    hours_per_day: Decimal | None = None,
    hourly_equivalent: Decimal | None = None,
) -> tuple[Decimal, Decimal | None]:
    """``(amount, rate_used)`` for a time item (timesheet entry or adjustment entry; signed quantity)."""
    if rate_unit == "hour":
        return quantize_amount(_mul(quantity, rate, multiplier), currency), rate
    if rate_unit == "day":
        if not hours_per_day:
            raise ValueError("hours_per_day required for day rates")
        product = _mul(quantity, rate, multiplier)
        return quantize_amount(MONEY_CONTEXT.divide(product, Decimal(hours_per_day)), currency), rate
    if rate_unit == "month":
        if category in MONTHLY_UNPRICED:
            return quantize_amount(ZERO, currency), None
        if hourly_equivalent is None:
            raise ValueError("hourly_equivalent required for OT on monthly rates")
        return quantize_amount(_mul(quantity, hourly_equivalent, multiplier), currency), Decimal(
            hourly_equivalent
        )
    raise ValueError(f"unsupported rate unit {rate_unit!r}")


def price_rate_adjustment(
    quantity: Decimal, rate_delta: Decimal, multiplier: Decimal, currency: str
) -> Decimal:
    return quantize_amount(_mul(quantity, rate_delta, multiplier), currency)


def monthly_base(rate: Decimal, proration_factor: Decimal, currency: str) -> Decimal:
    return quantize_amount(_mul(rate, proration_factor), currency)


@dataclasses.dataclass(frozen=True, slots=True)
class PricedItem:
    source_kind: str
    source_id: uuid.UUID | None
    placement_id: uuid.UUID | None
    candidate_id: uuid.UUID
    currency: str
    category: str | None
    item_date: date
    quantity: Decimal
    rate: Decimal | None
    multiplier: Decimal | None
    amount: Decimal
    rate_segment_id: uuid.UUID | None = None
    hours_per_day: Decimal | None = None

    @property
    def bucket(self) -> str:
        return bucket_for(self.source_kind)

    @property
    def postable(self) -> bool:
        return self.source_id is not None and self.source_kind != MONTHLY_BASE_KIND


@dataclasses.dataclass
class RowTotals:
    candidate_id: uuid.UUID
    currency: str
    placement_ids: list[uuid.UUID] = dataclasses.field(default_factory=list)
    hours: dict[str, Decimal] = dataclasses.field(
        default_factory=lambda: dict.fromkeys(HOUR_CATEGORIES, ZERO)
    )
    leave_days: dict[str, Decimal] = dataclasses.field(
        default_factory=lambda: {"leave_paid": ZERO, "leave_unpaid": ZERO}
    )
    earnings: dict[str, Decimal] = dataclasses.field(
        default_factory=lambda: dict.fromkeys((*EARNING_CATEGORIES, "base"), ZERO)
    )
    reimbursements_total: Decimal = ZERO
    adjustments_total: Decimal = ZERO
    rate_adjustments_total: Decimal = ZERO
    pay_rates: dict[str, str] = dataclasses.field(default_factory=dict)
    items: list[PricedItem] = dataclasses.field(default_factory=list)

    @property
    def gross_earnings_inputs(self) -> Decimal:
        """Earnings + adjustments + rate adjustments; reimbursements are reported separately."""
        return sum(self.earnings.values(), ZERO) + self.adjustments_total + self.rate_adjustments_total


def row_key(item: PricedItem) -> tuple[uuid.UUID, str]:
    return (item.candidate_id, item.currency)


def build_rows(items: Iterable[PricedItem]) -> list[RowTotals]:
    """Group items per (Worker, currency); totals are exact sums of rounded item amounts."""
    rows: dict[tuple[uuid.UUID, str], RowTotals] = {}
    days_raw: dict[tuple[uuid.UUID, str], dict[str, Decimal]] = {}
    for it in items:
        key = row_key(it)
        row = rows.get(key)
        if row is None:
            row = rows[key] = RowTotals(candidate_id=it.candidate_id, currency=it.currency)
            days_raw[key] = {"leave_paid": ZERO, "leave_unpaid": ZERO}
        row.items.append(it)
        if it.placement_id is not None and it.placement_id not in row.placement_ids:
            row.placement_ids.append(it.placement_id)
        b = it.bucket
        if b == "reimbursement":
            row.reimbursements_total += it.amount
        elif b == "adjustment":
            row.adjustments_total += it.amount
        elif b == "rate_adjustment":
            row.rate_adjustments_total += it.amount
        elif b == "base":
            row.earnings["base"] += it.amount
        else:
            cat = it.category if it.category in HOUR_CATEGORIES else "regular"
            row.hours[cat] += it.quantity
            if cat in row.earnings:
                row.earnings[cat] += it.amount
            if cat in ("leave_paid", "leave_unpaid") and it.hours_per_day:
                days_raw[key][cat] += MONEY_CONTEXT.divide(it.quantity, Decimal(it.hours_per_day))
        if it.rate is not None and it.rate_segment_id is not None and b in ("earning", "base"):
            row.pay_rates[str(it.rate_segment_id)] = format(it.rate, "f")
    for key, row in rows.items():
        row.leave_days = {k: quantize_hours(v) for k, v in days_raw[key].items()}
    return sorted(rows.values(), key=lambda r: (str(r.candidate_id), r.currency))


def totals_by_currency(rows: Sequence[RowTotals]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        t = out.setdefault(
            r.currency, {"gross_earnings_inputs": ZERO, "reimbursements_total": ZERO, "workers": ZERO}
        )
        t["gross_earnings_inputs"] += r.gross_earnings_inputs
        t["reimbursements_total"] += r.reimbursements_total
        t["workers"] += 1
    return {
        ccy: {k: (str(int(v)) if k == "workers" else format(v, "f")) for k, v in t.items()}
        for ccy, t in sorted(out.items())
    }


def items_hash(items: Iterable[PricedItem]) -> str:
    """SHA-256 of the sorted ``(source_kind, source_id, quantity, amount)`` tuples (monthly base items use
    ``placement:segment``)."""
    keys = sorted(
        [
            it.source_kind,
            str(it.source_id) if it.source_id else f"{it.placement_id}:{it.rate_segment_id}",
            format(Decimal(it.quantity), "f"),
            format(Decimal(it.amount), "f"),
        ]
        for it in items
    )
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()


def canonical_hash(payload: Any) -> str:
    """AD-21 ``content_hash``: SHA-256 of canonical JSON (sorted keys, Decimals as plain strings)."""

    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            return format(o, "f")
        if isinstance(o, (uuid.UUID, date)):
            return str(o)
        raise TypeError(type(o).__name__)

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=default).encode()
    ).hexdigest()
