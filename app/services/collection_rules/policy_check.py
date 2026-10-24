# ruff: file-ignore[line-too-long]
"""Deterministic dunning policy (Epic 63 A4). Pure: runs at proposal time (the tool preview) and again at execution,
so an edited Proposal cannot slip anything through.

* Forbidden language: legal threats, lawyers, courts, blacklisting / credit bureaus, discounts, waivers, interest-free
  offers, settlements, write-offs, stopping services.
* Every amount must equal a listed remindable / open amount or the total; every date must be a due date in the table
  (or an explicitly allowed date, e.g. the promised payment date of a broken promise).
* Links only to the tenant portal / statement.

``enforce`` raises ``ValidationFailed("dunning_policy_violation", details={"violations": [...]})``.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.core.errors import ValidationFailed

FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("legal_action", r"\blegal\s+(action|proceedings?|recourse|steps)\b"),
        ("legal_notice", r"\blegal\s+notice\b"),
        ("lawyer", r"\b(lawyers?|attorneys?|advocates?|solicitors?|counsel)\b"),
        ("court", r"\b(court|courts|litigation|sue|suing|lawsuit|arbitration)\b"),
        ("blacklist", r"\bblack[\s-]?list(ed|ing)?\b"),
        ("credit_bureau", r"\b(credit\s+bureaus?|cibil|credit\s+rating\s+agenc(y|ies)|experian|equifax|crif)\b"),
        ("discount", r"\bdiscount(s|ed)?\b"),
        ("waiver", r"\bwaiv(e|es|ed|er|ing)\b"),
        ("interest_free", r"\binterest[\s-]?free\b"),
        ("settlement", r"\bsettlements?\b"),
        ("write_off", r"\bwrit(e|ten|ing)[\s-]?off\b"),
        ("stop_services", r"\b(stop|suspend|halt|terminate|withdraw)\w*\s+(the\s+|our\s+|all\s+)?(services?|supply|deployment|consultants?|resources?)\b"),
    )
)
_CURRENCY_AMOUNT = re.compile(
    r"(?:₹|INR|Rs\.?|USD|US\$|\$|EUR|€|GBP|£|AED|SGD)\s?(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE
)
_PLAIN_AMOUNT = re.compile(r"(?<![\w.-])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2})(?![\w-])")  # NOSONAR
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TEXT_DATE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+(\d{4})\b", re.IGNORECASE
)
_LINK = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _dec(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _dates(text: str) -> list[date]:
    out: list[date] = []
    for y, m, d in _ISO_DATE.findall(text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            out.append(date.min)
    for d, mon, y in _TEXT_DATE.findall(text):
        try:
            out.append(date(int(y), _MONTHS[mon[:3].lower()], int(d)))
        except (ValueError, KeyError):
            out.append(date.min)
    return out


def check(
    text: str,
    *,
    amounts: Iterable[Decimal],
    due_dates: Iterable[date],
    extra_dates: Iterable[date] = (),
    link_prefixes: Iterable[str] = (),
) -> list[str]:
    """Violations of ``text`` (subject + body); empty = allowed."""
    violations: list[str] = []
    for label, pattern in FORBIDDEN:
        if pattern.search(text):
            violations.append(f"forbidden:{label}")
    allowed_amounts = {Decimal(a).quantize(Decimal("0.01")) for a in amounts}
    seen: set[Decimal] = set()
    for raw in [*_CURRENCY_AMOUNT.findall(text), *_PLAIN_AMOUNT.findall(text)]:
        value = _dec(raw)
        if value is None or value in seen:
            continue
        seen.add(value)
        if value not in allowed_amounts:
            violations.append(f"amount:{value}")
    allowed_dates = {*due_dates, *extra_dates}
    for d in _dates(text):
        if d not in allowed_dates:
            violations.append(f"date:{d.isoformat() if d != date.min else 'invalid'}")
    prefixes = tuple(p.rstrip("/").lower() for p in link_prefixes if p)
    for link in _LINK.findall(text):
        if not prefixes or not link.lower().startswith(prefixes):
            violations.append("link:not_allowed")
    return list(dict.fromkeys(violations))


def enforce(
    text: str,
    *,
    amounts: Iterable[Decimal],
    due_dates: Iterable[date],
    extra_dates: Iterable[date] = (),
    link_prefixes: Iterable[str] = (),
) -> None:
    violations = check(text, amounts=amounts, due_dates=due_dates, extra_dates=extra_dates, link_prefixes=link_prefixes)
    if violations:
        raise ValidationFailed(
            "The message breaks the collections policy", code="dunning_policy_violation", details={"violations": violations}
        )


def as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


__all__ = ["FORBIDDEN", "as_date", "check", "enforce"]
