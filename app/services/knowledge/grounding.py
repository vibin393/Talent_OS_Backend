# ruff: noqa: E501
"""Deterministic grounding check of an onboarding answer (Epic 62 A2; pure, hypothesis-tested).

``check_answer(answer, cited_texts, record_facts)`` extracts the factual claims a model could invent -- amounts of
money (₹ / INR / Rs / lakh / crore / LPA), dates (absolute dates, weekdays, months), percentages, durations and decision
phrases ("approved", "you will be paid", ...) -- and requires each to appear (normalised) in a cited text or in the
asker's own record facts (task labels, due dates, statuses, the case start date). It also refuses links that are not
in the cited text, answers without citations and answers over 1,500 characters. Violations name the token type only
(never the token itself).
"""

import dataclasses
import re
from collections.abc import Iterable, Sequence

MAX_ANSWER = 1500
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DECISIONS = (
    "approved", "rejected", "waived", "confirmed", "you will be paid", "you'll be paid", "guaranteed", "guarantee",
    "you are selected", "you have been selected", "verified",
)  # fmt: skip
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?:(?:₹|\binr\b|\brs\.?|\$|\busd\b)\s*({_NUM}))|(?:({_NUM})\s*(?:lakhs?|lacs?|crores?|lpa|k\b|/-|rupees|inr\b))",
    re.IGNORECASE,
)
_PERCENT = re.compile(rf"({_NUM})\s*(?:%|percent\b|per cent\b)", re.IGNORECASE)
_DURATION = re.compile(
    r"\b(\d+)\s*(working days?|business days?|days?|weeks?|months?|years?|hours?|hrs?)\b", re.IGNORECASE
)
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b\.?", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_MONTH_ONLY = re.compile(rf"\b({_MONTH_RE})\b", re.IGNORECASE)
_WEEKDAY = re.compile(rf"\b({'|'.join(WEEKDAYS)})\b", re.IGNORECASE)
_LINK = re.compile(r"(https?://[^\s)\]>]+|\bwww\.[^\s)\]>]+)", re.IGNORECASE)
_NUMBER = re.compile(_NUM)


@dataclasses.dataclass(frozen=True, slots=True)
class GroundingResult:
    ok: bool
    violations: tuple[str, ...] = ()


def _num(raw: str) -> str:
    value = raw.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


def _numbers(text: str) -> set[str]:
    return {_num(m.group(0)) for m in _NUMBER.finditer(text)}


def money(text: str) -> set[str]:
    return {_num(m.group(1) or m.group(2)) for m in _MONEY.finditer(text)}


def percentages(text: str) -> set[str]:
    return {_num(m.group(1)) for m in _PERCENT.finditer(text)}


def _unit(raw: str) -> str:
    u = raw.lower()
    if u.startswith(("working", "business")):
        return "working_day"
    if u.startswith("hr") or u.startswith("hour"):
        return "hour"
    return u.rstrip("s")


def durations(text: str) -> set[tuple[str, str]]:
    return {(_num(m.group(1)), _unit(m.group(2))) for m in _DURATION.finditer(text)}


def dates(text: str) -> set[str]:
    """Normalised date tokens: ``m-d`` for dated forms (year ignored), weekday names, ``month:<m>``."""
    out: set[str] = set()
    spans: list[tuple[int, int]] = []
    for m in _ISO.finditer(text):
        out.add(f"{int(m.group(2))}-{int(m.group(3))}")
        spans.append(m.span())
    for m in _DMY.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        out.add(f"{int(m.group(2))}-{int(m.group(1))}")
        spans.append(m.span())
    for m in _DAY_MONTH.finditer(text):
        out.add(f"{MONTHS[m.group(2).lower()]}-{int(m.group(1))}")
        spans.append(m.span())
    for m in _MONTH_DAY.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        out.add(f"{MONTHS[m.group(1).lower()]}-{int(m.group(2))}")
        spans.append(m.span())
    for m in _MONTH_ONLY.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        word = m.group(1).lower()
        if word == "may" and not text[m.start() : m.start() + 1].isupper():
            continue  # the verb "may"
        out.add(f"month:{MONTHS[word]}")
    for m in _WEEKDAY.finditer(text):
        out.add(m.group(1).lower())
    return out


def _dated_months(tokens: Iterable[str]) -> set[str]:
    return {f"month:{t.split('-')[0]}" for t in tokens if "-" in t and not t.startswith("month:")}


def decisions(text: str) -> set[str]:
    low = text.lower()
    return {d for d in DECISIONS if re.search(rf"\b{re.escape(d)}\b", low)}


def links(text: str) -> set[str]:
    return {m.group(0).rstrip(".,;:").lower() for m in _LINK.finditer(text)}


def check_answer(answer: str, cited_texts: Sequence[str], record_facts: Sequence[str]) -> GroundingResult:
    violations: list[str] = []
    if not cited_texts and not record_facts:
        violations.append("no_citations")
    if len(answer) > MAX_ANSWER:
        violations.append("too_long")
    support = "\n".join([*cited_texts, *record_facts])
    support_numbers = _numbers(support)
    if any(v not in support_numbers for v in money(answer)):
        violations.append("money")
    if any(v not in percentages(support) and v not in support_numbers for v in percentages(answer)):
        violations.append("percentage")
    support_durations = durations(support)
    if any(d not in support_durations for d in durations(answer)):
        violations.append("duration")
    support_dates = dates(support)
    support_dates |= _dated_months(support_dates)
    if any(d not in support_dates for d in dates(answer)):
        violations.append("date")
    support_low = support.lower()
    if any(d not in support_low for d in decisions(answer)):
        violations.append("decision")
    support_links = links(support)
    if any(link not in support_links for link in links(answer)):
        violations.append("link")
    return GroundingResult(ok=not violations, violations=tuple(dict.fromkeys(violations)))


__all__ = [
    "DECISIONS",
    "MAX_ANSWER",
    "GroundingResult",
    "check_answer",
    "dates",
    "decisions",
    "durations",
    "links",
    "money",
    "percentages",
]
