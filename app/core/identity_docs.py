# ruff: noqa: D401, E501
"""Identity-document rules shared by every domain (Epic 21 A2, NFR-24, AD-35). Pure: no tables, no I/O.

* Aadhaar is stored only as its **last 4 digits** plus a redacted image. ``looks_like_full_aadhaar`` detects a
  full 12-digit Aadhaar number (optionally grouped 4-4-4 with spaces / hyphens) that passes the Verhoeff
  checksum and does not start with 0 or 1; ``assert_no_full_aadhaar`` refuses free text carrying one (422
  ``aadhaar_full_number_forbidden``). Compliance, BGV and onboarding call it on every free-text field.
* ``mask_full_aadhaar`` is applied to every log line by ``core.logger`` (``XXXX-XXXX-1234``).
* Redaction review states of an Aadhaar image: ``pending_review`` -> ``confirmed`` | ``rejected``.
"""

import re

from app.core.errors import ValidationFailed

REDACTION_STATES = ("pending_review", "confirmed", "rejected")

PASSPORT_RE = re.compile(r"^[A-Z][0-9]{7}$")  # Indian passport
GENERIC_NUMBER_RE = re.compile(r"^[A-Z0-9]{1,30}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_LAST4_RE = re.compile(r"^[0-9]{4}$")
# 12 digits, either contiguous or grouped 4-4-4 with one consistent-ish separator; not embedded in a longer number.
_AADHAAR_CANDIDATE = re.compile(r"(?<![0-9])([2-9][0-9]{3})[ \-]?([0-9]{4})[ \-]?([0-9]{4})(?![0-9])")

# Verhoeff tables
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(digits: str) -> str:
    """The Verhoeff check digit to append to ``digits`` (test-data helper)."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def _matches(text: str) -> list[re.Match[str]]:
    return [m for m in _AADHAAR_CANDIDATE.finditer(text) if verhoeff_valid("".join(m.groups()))]


def looks_like_full_aadhaar(text: str | None) -> bool:
    return bool(text) and bool(_matches(str(text)))


def mask_full_aadhaar(text: str) -> str:
    """Replace every full Aadhaar number in ``text`` by ``XXXX-XXXX-<last4>``."""
    if not text or not any(ch.isdigit() for ch in text):
        return text
    return _AADHAAR_CANDIDATE.sub(
        lambda m: f"XXXX-XXXX-{m.group(3)}" if verhoeff_valid("".join(m.groups())) else m.group(0), text
    )


def assert_no_full_aadhaar(*texts: object) -> None:
    for value in texts:
        if isinstance(value, str) and looks_like_full_aadhaar(value):
            raise ValidationFailed(
                "Do not enter a full Aadhaar number; only the last 4 digits may be stored",
                code="aadhaar_full_number_forbidden",
            )


def validate_aadhaar_last4(value: str | None) -> str:
    v = (value or "").strip()
    if not _LAST4_RE.match(v):
        raise ValidationFailed(
            "Enter exactly the last 4 digits of the Aadhaar number", code="aadhaar_last4_invalid"
        )
    return v


def mask_last4(value: str | None) -> str:
    v = re.sub(r"[\s\-]", "", value or "")
    return f"XXXX{v[-4:]}" if v else ""


__all__ = [
    "PAN_RE",
    "PASSPORT_RE",
    "REDACTION_STATES",
    "assert_no_full_aadhaar",
    "looks_like_full_aadhaar",
    "mask_full_aadhaar",
    "mask_last4",
    "validate_aadhaar_last4",
    "verhoeff_check_digit",
    "verhoeff_valid",
]
