# ruff: file-ignore[line-too-long]
"""The single GST function of the code base (AD-19, FR-146). Pure: no DB, no I/O, no clock.

``determine_supply`` decides place of supply, supply type, intra/inter state, UTGST, zero rating (LUT) and
reverse charge; ``compute_line_tax`` turns one line's taxable value into tax components; ``inr_tax`` applies the
same formula to the INR taxable value (AD-20: INR tax is never ``foreign_tax x fx``). Every component amount is
quantized per line, half-up (``core.money``). Callers resolve tax rates (``rates.rate_for``) and LUTs.
"""

import dataclasses
from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

from app.core.money import MONEY_CONTEXT, quantize_amount, to_decimal

SupplyType = Literal["b2b", "b2c", "sezwp", "sezwop", "expwp", "expwop", "outside_gst"]
Treatment = Literal["registered", "unregistered", "sez", "export"]
Component = Literal["cgst", "sgst", "utgst", "igst"]

EXPORT_POS = "96"
# Union territories without legislature (UTGST instead of SGST): Chandigarh, DNH & DD (old and merged codes),
# Lakshadweep, Andaman & Nicobar, Ladakh and "Other Territory".
UT_WITHOUT_LEGISLATURE: frozenset[str] = frozenset({"04", "25", "26", "31", "35", "38", "97"})
ZERO_RATED: frozenset[str] = frozenset({"sezwop", "expwop"})
EINVOICE_SUPPLY_TYPES: frozenset[str] = frozenset({"b2b", "sezwp", "sezwop", "expwp", "expwop"})


class TaxDecisionError(Exception):
    """The supply cannot be decided (``place_of_supply_unresolved``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True, slots=True)
class SupplierReg:
    gstin: str
    state_code: str


@dataclasses.dataclass(frozen=True, slots=True)
class Recipient:
    gstin: str | None
    state_code: str | None
    country: str = "IN"


@dataclasses.dataclass(frozen=True, slots=True)
class Lut:
    lut_number: str
    fiscal_year: str


@dataclasses.dataclass(frozen=True, slots=True)
class SupplyDecision:
    pos_state_code: str | None
    supply_type: SupplyType
    intra_state: bool
    use_utgst: bool
    zero_rated: bool
    lut_ref: str | None
    reverse_charge: bool
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "pos_state_code": self.pos_state_code,
            "supply_type": self.supply_type,
            "intra_state": self.intra_state,
            "use_utgst": self.use_utgst,
            "zero_rated": self.zero_rated,
            "lut_ref": self.lut_ref,
            "reverse_charge": self.reverse_charge,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SupplyDecision":
        return cls(
            pos_state_code=data.get("pos_state_code"),  # type: ignore[arg-type]
            supply_type=data["supply_type"],  # type: ignore[arg-type]
            intra_state=bool(data.get("intra_state")),
            use_utgst=bool(data.get("use_utgst")),
            zero_rated=bool(data.get("zero_rated")),
            lut_ref=data.get("lut_ref"),  # type: ignore[arg-type]
            reverse_charge=bool(data.get("reverse_charge")),
            warnings=tuple(data.get("warnings") or ()),  # type: ignore[arg-type]
        )


@dataclasses.dataclass(frozen=True, slots=True)
class TaxComponent:
    component: Component
    rate_pct: Decimal
    amount: Decimal
    charged: bool


def determine_supply(
    supplier: SupplierReg | None,
    recipient: Recipient,
    treatment: Treatment,
    lut: Lut | None,
    reverse_charge: bool,
    *,
    ut_without_legislature: Iterable[str] = UT_WITHOUT_LEGISLATURE,
) -> SupplyDecision:
    """Place of supply and supply type (IGST Act s.7 / s.12; LUT = the supplier registration's LUT for the
    document's financial year, or ``None``)."""
    if supplier is None:
        return SupplyDecision(None, "outside_gst", False, False, False, None, False)
    lut_ref = lut.lut_number if lut is not None else None
    if treatment == "export" or (recipient.country or "IN").upper() != "IN":
        if lut_ref:
            return SupplyDecision(EXPORT_POS, "expwop", False, False, True, lut_ref, False)
        return SupplyDecision(EXPORT_POS, "expwp", False, False, False, None, False, ("lut_missing",))
    if treatment == "sez":
        pos = recipient.state_code or supplier.state_code
        if lut_ref:
            return SupplyDecision(pos, "sezwop", False, False, True, lut_ref, False)
        return SupplyDecision(pos, "sezwp", False, False, False, None, False)
    registered = treatment == "registered" and bool(recipient.gstin)
    pos = recipient.state_code  # type: ignore[assignment]
    if not pos:
        if registered:
            raise TaxDecisionError(
                "place_of_supply_unresolved", "The recipient's billing address has no state (place of supply)"
            )
        pos = supplier.state_code
    intra = pos == supplier.state_code
    use_utgst = intra and pos in set(ut_without_legislature)
    return SupplyDecision(
        pos, "b2b" if registered else "b2c", intra, use_utgst, False, None, bool(reverse_charge)
    )


def _pct(amount: Decimal, rate_pct: Decimal, currency: str) -> Decimal:
    product = MONEY_CONTEXT.multiply(to_decimal(amount), to_decimal(rate_pct))
    return quantize_amount(MONEY_CONTEXT.divide(product, Decimal(100)), currency)


def compute_line_tax(
    taxable: Decimal, rate_pct: Decimal, decision: SupplyDecision, currency: str, *, pure_agent: bool = False
) -> list[TaxComponent]:
    """Tax components of one line (per-line rounding, half-up). Reverse charge: same amounts, ``charged=False``."""
    if decision.supply_type == "outside_gst" or pure_agent:
        return []
    rate = to_decimal(rate_pct)
    charged = not decision.reverse_charge
    if decision.zero_rated:
        return [TaxComponent("igst", Decimal("0"), quantize_amount(Decimal(0), currency), charged)]
    if decision.intra_state:
        half = MONEY_CONTEXT.divide(rate, Decimal(2))
        amount = _pct(taxable, half, currency)
        second: Component = "utgst" if decision.use_utgst else "sgst"
        return [TaxComponent("cgst", half, amount, charged), TaxComponent(second, half, amount, charged)]
    return [TaxComponent("igst", rate, _pct(taxable, rate, currency), charged)]


def inr_tax(
    inr_taxable: Decimal, rate_pct: Decimal, decision: SupplyDecision, *, pure_agent: bool = False
) -> dict[str, Decimal]:
    """INR amount per component: ``quantize(inr_taxable x component rate)`` (never ``foreign tax x fx``)."""
    return {
        c.component: c.amount
        for c in compute_line_tax(inr_taxable, rate_pct, decision, "INR", pure_agent=pure_agent)
    }


def einvoice_applicable(supply_type: str, registration_applicable: bool) -> bool:
    return registration_applicable and supply_type in EINVOICE_SUPPLY_TYPES
