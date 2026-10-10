"""Pure expense arithmetic (AD-20): Decimal only, ``ROUND_HALF_UP``, every line rounded ONCE to the claim
currency's minor units. No I/O.

* Mileage: ``quantize(km x rate_per_km, claim_ccy)``.
* FX: ``quantize(amount x fx, claim_ccy)``; same currency -> the amount unchanged.
* Re-bill: ``at_cost`` -> the claim-currency amount; ``markup`` -> ``quantize(amount x (1 + pct/100))``;
  ``not_billable`` -> ``None``.
* Limits: a total is over a limit only when strictly greater (a total equal to the limit is allowed).
"""

from decimal import Decimal, InvalidOperation

from app.core.errors import ValidationFailed
from app.core.money import MONEY_CONTEXT, minor_units, quantize_amount, to_decimal

ZERO = Decimal("0")
HUNDRED = Decimal(100)


def check_currency(currency: str) -> str:
    """Expense currencies must exist and have 0 or 2 minor units (else 422 ``currency_unsupported``)."""
    code = (currency or "").strip().upper()
    try:
        units = minor_units(code)
    except ValueError:
        units = -1
    if units not in (0, 2):
        raise ValidationFailed(
            "This currency cannot be used for expenses",
            code="currency_unsupported",
            details={"currency": code},
        )
    return code


def parse_amount(raw: object, currency: str, *, field: str = "amount") -> Decimal:
    """Strict decimal parsing of a request amount: never a float; negative -> 422 ``negative_amount``; more
    decimal places than the currency allows -> 422 ``amount_invalid``."""
    if not isinstance(raw, str | Decimal):
        raise ValidationFailed(
            "Amounts must be decimal strings", code="amount_invalid", details={"field": field}
        )
    try:
        value = to_decimal(raw)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValidationFailed(
            "Enter a valid amount", code="amount_invalid", details={"field": field}
        ) from exc
    if value < 0:
        raise ValidationFailed(
            "Expense amounts cannot be negative", code="negative_amount", details={"field": field}
        )
    exponent = value.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if places > minor_units(currency):
        raise ValidationFailed(
            "Too many decimal places for this currency", code="amount_invalid", details={"field": field}
        )
    return value


def parse_km(raw: str | Decimal) -> Decimal:
    try:
        value = to_decimal(raw)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValidationFailed("Enter the distance in km", code="km_invalid") from exc
    if value < 0:
        raise ValidationFailed(
            "Distances cannot be negative", code="negative_amount", details={"field": "km"}
        )
    return value


def mileage_amount(km: Decimal, rate_per_km: Decimal, claim_currency: str) -> Decimal:
    return quantize_amount(MONEY_CONTEXT.multiply(to_decimal(km), to_decimal(rate_per_km)), claim_currency)


def convert(amount: Decimal, fx_rate: Decimal | None, claim_currency: str) -> Decimal:
    """Line amount in the claim currency, rounded once (``fx_rate`` ``None`` = same currency)."""
    if fx_rate is None:
        return quantize_amount(to_decimal(amount), claim_currency)
    return quantize_amount(MONEY_CONTEXT.multiply(to_decimal(amount), to_decimal(fx_rate)), claim_currency)


def bill_amount(
    amount_claim_ccy: Decimal, rebill_method: str | None, markup_pct: Decimal | None, claim_currency: str
) -> Decimal | None:
    if rebill_method in (None, "not_billable"):
        return None
    if rebill_method == "markup":
        pct = to_decimal(markup_pct if markup_pct is not None else ZERO)
        factor = MONEY_CONTEXT.add(Decimal(1), MONEY_CONTEXT.divide(pct, HUNDRED))
        return quantize_amount(MONEY_CONTEXT.multiply(to_decimal(amount_claim_ccy), factor), claim_currency)
    return quantize_amount(to_decimal(amount_claim_ccy), claim_currency)


def over_limit(total: Decimal, limit: Decimal | None) -> bool:
    return limit is not None and to_decimal(total) > to_decimal(limit)


def receipt_required(amount_claim_ccy: Decimal, threshold: Decimal | None) -> bool:
    """``threshold`` ``None`` = never; ``0`` = always (for any positive amount)."""
    return threshold is not None and to_decimal(amount_claim_ccy) > to_decimal(threshold)


def total(values: list[Decimal]) -> Decimal:
    out = Decimal("0.00")
    for v in values:
        out = MONEY_CONTEXT.add(out, to_decimal(v))
    return out


def parse_suggested_amount(raw: object) -> Decimal | None:
    """AI receipt suggestion amount: a plain decimal string only (``"1234.50"``). Thousands separators,
    floats, signs or anything else -> ``None`` (the suggestion is dropped, never parsed as float)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or any(ch not in "0123456789." for ch in text) or text.count(".") > 1:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None
