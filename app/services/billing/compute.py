# ruff: noqa: E501
"""Pure document arithmetic (AD-19 / AD-20): line pricing -> tax components -> INR snapshot -> totals, plus the
preview fingerprint and the canonical content hash. No DB, no I/O, no clock; re-running it on stored inputs is
byte-identical (property-tested)."""

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.money import MONEY_CONTEXT, quantize_amount, quantize_fx, to_decimal
from app.services.billing import pricing, tax

ZERO = Decimal("0.00")


@dataclasses.dataclass(frozen=True, slots=True)
class LineInput:
    line_kind: str
    description: str
    sac_code: str
    quantity: Decimal
    rate: Decimal
    multiplier: Decimal = Decimal(1)
    discount: Decimal = ZERO
    unit: str = "item"
    pure_agent: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class LineCalc:
    effective_rate: Decimal
    gross: Decimal
    discount: Decimal
    taxable: Decimal
    rate_pct: Decimal
    components: tuple[tax.TaxComponent, ...]
    inr_taxable: Decimal
    inr_components: Mapping[str, Decimal]
    outside: bool


@dataclasses.dataclass(frozen=True, slots=True)
class DocCalc:
    lines: tuple[LineCalc, ...]
    taxable_total: Decimal
    outside_taxable_total: Decimal
    charged_tax_total: Decimal
    rcm_tax_total: Decimal
    round_off: Decimal
    document_total: Decimal
    inr_taxable_total: Decimal
    inr_tax_total: Decimal
    inr_document_total: Decimal


def inr(amount: Decimal, fx_rate: Decimal) -> Decimal:
    return quantize_amount(MONEY_CONTEXT.multiply(to_decimal(amount), to_decimal(fx_rate)), "INR")


def compute_line(
    line: LineInput, rate_pct: Decimal, decision: tax.SupplyDecision, currency: str, fx_rate: Decimal
) -> LineCalc:
    eff = pricing.effective_rate(line.rate, line.multiplier)
    gross = pricing.line_amount(line.quantity, line.rate, line.multiplier, currency)
    taxable = pricing.taxable(gross, line.discount, currency)
    components = tax.compute_line_tax(taxable, rate_pct, decision, currency, pure_agent=line.pure_agent)
    inr_taxable = inr(taxable, fx_rate)
    inr_components = tax.inr_tax(inr_taxable, rate_pct, decision, pure_agent=line.pure_agent)
    return LineCalc(
        effective_rate=eff,
        gross=gross,
        discount=quantize_amount(line.discount, currency),
        taxable=taxable,
        rate_pct=Decimal(0)
        if line.pure_agent or decision.supply_type == "outside_gst"
        else to_decimal(rate_pct),
        components=tuple(components),
        inr_taxable=inr_taxable,
        inr_components=inr_components,
        outside=line.pure_agent,
    )


def totals(
    lines: Sequence[LineCalc],
    currency: str,
    *,
    round_off_mode: str = "none",
    round_off: Decimal | None = None,
) -> DocCalc:
    taxable_total = sum((c.taxable for c in lines if not c.outside), ZERO)
    outside_total = sum((c.taxable for c in lines if c.outside), ZERO)
    charged = sum((t.amount for c in lines for t in c.components if t.charged), ZERO)
    rcm = sum((t.amount for c in lines for t in c.components if not t.charged), ZERO)
    before = taxable_total + outside_total + charged
    ro = round_off if round_off is not None else pricing.round_off(before, round_off_mode, currency)
    inr_taxable_total = sum((c.inr_taxable for c in lines if not c.outside), ZERO)
    inr_outside = sum((c.inr_taxable for c in lines if c.outside), ZERO)
    charged_flags = {t.component: t.charged for c in lines for t in c.components}
    inr_tax_total = sum(
        (amt for c in lines for comp, amt in c.inr_components.items() if charged_flags.get(comp, True)), ZERO
    )
    inr_round = ro if currency == "INR" else ZERO
    return DocCalc(
        lines=tuple(lines),
        taxable_total=quantize_amount(taxable_total, currency),
        outside_taxable_total=quantize_amount(outside_total, currency),
        charged_tax_total=quantize_amount(charged, currency),
        rcm_tax_total=quantize_amount(rcm, currency),
        round_off=quantize_amount(ro, currency),
        document_total=quantize_amount(before + ro, currency),
        inr_taxable_total=inr_taxable_total,
        inr_tax_total=inr_tax_total,
        inr_document_total=quantize_amount(
            inr_taxable_total + inr_outside + inr_tax_total + inr_round, "INR"
        ),
    )


def compute_document(
    lines: Sequence[LineInput],
    rates: Mapping[str, Decimal],
    decision: tax.SupplyDecision,
    currency: str,
    fx_rate: Decimal,
    *,
    round_off_mode: str = "none",
) -> DocCalc:
    calcs = [
        compute_line(li, rates.get(li.sac_code, Decimal(0)), decision, currency, fx_rate) for li in lines
    ]
    return totals(calcs, currency, round_off_mode=round_off_mode)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (uuid.UUID, date, datetime)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def canonical_json(data: Any) -> str:
    return json.dumps(_jsonable(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def fingerprint(decision: tax.SupplyDecision, calc: DocCalc, fx_rate: Decimal) -> str:
    """Fingerprint what an approver saw: supply decision, rates, per-line taxable / tax and FX (no dates)."""
    return sha256(
        {
            "decision": decision.as_dict(),
            "fx_rate": quantize_fx(to_decimal(fx_rate)),
            "lines": [
                {
                    "taxable": c.taxable,
                    "rate_pct": c.rate_pct,
                    "tax": [(t.component, t.rate_pct, t.amount, t.charged) for t in c.components],
                }
                for c in calc.lines
            ],
            "total": calc.document_total,
        }
    )


def diff(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    old = old or {}
    return {k: {"from": _jsonable(old.get(k)), "to": _jsonable(v)} for k, v in new.items() if old.get(k) != v}
