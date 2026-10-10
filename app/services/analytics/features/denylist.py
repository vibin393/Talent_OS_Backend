"""Protected-feature denylist (Epic 48 A3, FR-244, NFR-37).

Token-based: a spec ``key``, ``label`` or ``source`` is split on non-alphanumerics (``candidate.pincode`` ->
``candidate``, ``pincode``) and refused when any token -- or any adjacent token pair (``date_of_birth``,
``first_name``) -- is denylisted. Used by ``check_spec`` (training refuses a bad spec) and by the CI test.
"""

import itertools
import re
from collections.abc import Iterable, Mapping
from typing import Any

# category label -> tokens
CATEGORIES: Mapping[str, frozenset[str]] = {
    "Protected attributes (gender, age, religion, caste, marital status, nationality, disability)": frozenset(
        {
            "gender",
            "sex",
            "age",
            "dob",
            "birth",
            "birthdate",
            "date_of_birth",
            "marital",
            "religion",
            "caste",
            "nationality",
            "disability",
            "disabled",
            "pregnancy",
            "ethnicity",
            "race",
        }
    ),
    "Identity proxies (name, photo, email, phone)": frozenset(
        {"name", "first_name", "last_name", "full_name", "photo", "email", "phone", "mobile"}
    ),
    "Self-identification and fairness data": frozenset(
        {"demographic", "self_id", "selfid", "demographic_self_id", "fairness"}
    ),
    "Location below city level (pincode, address, coordinates)": frozenset(
        {
            "pincode",
            "pin",
            "postal",
            "postcode",
            "zip",
            "zipcode",
            "address",
            "latitude",
            "longitude",
            "lat",
            "lng",
            "locality",
            "geo",
        }
    ),
    "Free text (notes, feedback, resume, job description, comments)": frozenset(
        {
            "note",
            "notes",
            "feedback_text",
            "comment",
            "comments",
            "resume",
            "resume_text",
            "jd",
            "jd_text",
            "description",
            "free_text",
            "text",
            "body",
            "summary",
        }
    ),
}
TOKENS: frozenset[str] = frozenset().union(*CATEGORIES.values())
EXCLUDED_CATEGORIES: tuple[str, ...] = tuple(CATEGORIES)
_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(value: str) -> list[str]:
    parts = [p for p in _SPLIT.split(value.lower()) if p]
    pairs = [f"{a}_{b}" for a, b in itertools.pairwise(parts)]
    triples = [f"{a}_{b}_{c}" for a, b, c in zip(parts, parts[1:], parts[2:], strict=False)]
    return parts + pairs + triples


def hits(value: str) -> list[str]:
    return sorted({t for t in tokens(value) if t in TOKENS})


def violations(features: Iterable[Mapping[str, Any]]) -> list[str]:
    """``["<key>: <field> contains <token>"]`` for every denylisted token in a spec's key, label or source."""
    out: list[str] = []
    for f in features:
        for field in ("key", "label", "source"):
            value = str(f.get(field) or "")
            out.extend(f"{f.get('key')}: {field} contains {token!r}" for token in hits(value))
    return out


def text_violations(text: str) -> list[str]:
    """Denylisted tokens in free text (an AI explanation). Only the protected / identity / self-ID categories:
    an explanation may legitimately say "feedback" or "notes"."""
    banned = (
        CATEGORIES[
            "Protected attributes (gender, age, religion, caste, marital status, nationality, disability)"
        ]
        | CATEGORIES["Self-identification and fairness data"]
        | {"pincode", "postal", "address", "photo", "caste", "religion"}
    )
    return sorted({t for t in tokens(text) if t in banned})


class DenylistViolation(ValueError):  # noqa: N818 - domain name
    pass


def check_spec(features: Iterable[Mapping[str, Any]]) -> None:
    found = violations(features)
    if found:
        raise DenylistViolation("; ".join(found))
