# ruff: file-ignore[line-too-long]
"""Amounts in words (FR-149, Rule 46): Indian numbering (thousand / lakh / crore). Pure."""

from decimal import Decimal

from app.core.money import minor_units, quantize_amount, to_decimal

_ONES = [
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = ["_", "_", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
_UNITS: dict[str, tuple[str, str]] = {
    "INR": ("Indian Rupees", "Paise"),
    "USD": ("US Dollars", "Cents"),
    "EUR": ("Euros", "Cents"),
    "GBP": ("Pounds Sterling", "Pence"),
    "AED": ("UAE Dirhams", "Fils"),
    "SGD": ("Singapore Dollars", "Cents"),
    "AUD": ("Australian Dollars", "Cents"),
    "CAD": ("Canadian Dollars", "Cents"),
    "JPY": ("Japanese Yen", ""),
}


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def integer_words(n: int) -> str:
    """Indian system: 12345678 -> 'One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight'."""
    if n < 0:
        return "Minus " + integer_words(-n)
    if n == 0:
        return "Zero"
    parts: list[str] = []
    crore, n = divmod(n, 10_000_000)
    if crore:
        parts.append(f"{integer_words(crore)} Crore")
    lakh, n = divmod(n, 100_000)
    if lakh:
        parts.append(f"{_below_hundred(lakh)} Lakh")
    thousand, n = divmod(n, 1000)
    if thousand:
        parts.append(f"{_below_hundred(thousand)} Thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(amount: Decimal, currency: str) -> str:
    value = quantize_amount(to_decimal(amount), currency)
    major_name, minor_name = _UNITS.get(currency.upper(), (currency.upper(), "Cents"))
    sign = "Minus " if value < 0 else ""
    value = abs(value)
    major = int(value)
    places = minor_units(currency)
    minor = int(((value - major) * (10**places)).to_integral_value()) if places else 0
    text = f"{sign}{major_name} {integer_words(major)}"
    if minor and minor_name:
        text += f" and {integer_words(minor)} {minor_name}"
    return text + " Only"


def indian_grouping(amount: Decimal) -> str:
    """'1234567.50' -> '12,34,567.50'."""
    value = to_decimal(amount)
    sign = "-" if value < 0 else ""
    text = f"{abs(value):f}"
    whole, _, frac = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return sign + whole + (f".{frac}" if frac else "")


__all__ = ["Decimal", "amount_in_words", "indian_grouping", "integer_words"]
