"""Biased / discriminatory JD wording (FR-14, story 4.3).

``DEFAULT_BIASED_TERMS`` ships with the product; a tenant adds terms or disables defaults through
``biased_terms`` rows. ``check(text, terms)`` is pure: case-insensitive whole-word (phrase) matching.
"""

import re
from collections.abc import Iterable
from typing import TypedDict

CATEGORIES: tuple[str, ...] = (
    "age",
    "gender",
    "marital",
    "religion",
    "caste",
    "nationality",
    "disability",
    "appearance",
    "other",
)

DEFAULT_BIASED_TERMS: tuple[tuple[str, str], ...] = (
    # age
    ("young", "age"),
    ("youthful", "age"),
    ("young and dynamic", "age"),
    ("recent graduate", "age"),
    ("digital native", "age"),
    ("energetic youngster", "age"),
    ("below 30", "age"),
    ("under 30", "age"),
    ("under 35", "age"),
    ("age limit", "age"),
    # gender
    ("female only", "gender"),
    ("male only", "gender"),
    ("females only", "gender"),
    ("males only", "gender"),
    ("only male", "gender"),
    ("only female", "gender"),
    ("male candidates", "gender"),
    ("female candidates", "gender"),
    ("salesman", "gender"),
    ("chairman", "gender"),
    ("manpower", "gender"),
    ("he/she", "gender"),
    # marital
    ("unmarried", "marital"),
    ("married", "marital"),
    ("bachelor only", "marital"),
    ("no children", "marital"),
    # religion
    ("hindu only", "religion"),
    ("muslim only", "religion"),
    ("christian only", "religion"),
    ("religious", "religion"),
    # caste
    ("caste", "caste"),
    ("brahmin", "caste"),
    ("upper caste", "caste"),
    # nationality / origin
    ("native speaker", "nationality"),
    ("locals only", "nationality"),
    ("citizens only", "nationality"),
    # disability
    ("able-bodied", "disability"),
    ("physically fit", "disability"),
    ("no disabilities", "disability"),
    ("normal eyesight", "disability"),
    # appearance
    ("good looking", "appearance"),
    ("attractive", "appearance"),
    ("fair complexion", "appearance"),
    # Epic 55 (FR-282): gendered and age-coded job-ad wording (additive; tenant ``biased_terms`` unchanged).
    ("energetic graduate", "age"),
    ("rockstar", "other"),
    ("rock star", "other"),
    ("ninja", "other"),
    ("he/him", "gender"),
    ("he will", "gender"),
    ("his team", "gender"),
)


class BiasMatch(TypedDict):
    term: str
    category: str
    start: int
    end: int


def _pattern(term: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE)


def effective_terms(overrides: Iterable[tuple[str, str, bool]]) -> list[tuple[str, str]]:
    """Defaults plus tenant ``(term, category, enabled)`` rows; a disabled row removes a default."""
    terms: dict[str, tuple[str, str]] = {t.lower(): (t, c) for t, c in DEFAULT_BIASED_TERMS}
    for term, category, enabled in overrides:
        key = " ".join(term.split()).lower()
        if enabled:
            terms[key] = (term, category)
        else:
            terms.pop(key, None)
    return list(terms.values())


def check(text: str, terms: Iterable[tuple[str, str]]) -> list[BiasMatch]:
    """All matches, ordered by position; overlapping shorter matches inside a longer one are dropped."""
    found: list[BiasMatch] = []
    for term, category in terms:
        if not term.strip():
            continue
        for m in _pattern(term).finditer(text or ""):
            found.append(BiasMatch(term=term, category=category, start=m.start(), end=m.end()))
    found.sort(key=lambda m: (m["start"], -(m["end"] - m["start"])))
    out: list[BiasMatch] = []
    for match in found:
        if out and match["start"] < out[-1]["end"]:
            continue
        out.append(match)
    return out
