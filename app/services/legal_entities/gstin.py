"""GSTIN validation (AD-17): the only implementation in the code base. Pure, no I/O.

Format ``SS PPPPPPPPPP E Z C``: 2-digit state code, the holder's PAN, entity number (1-9 / A-Z), ``Z`` and a
mod-36 check character.
"""

import re

from app.core.errors import FieldError

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def check_character(first14: str) -> str:
    """The mod-36 check character for the first 14 characters of a GSTIN."""
    total = 0
    for i, ch in enumerate(first14):
        product = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36]


def validate_gstin(gstin: str, *, state_code: str | None = None, pan: str | None = None) -> list[FieldError]:
    value = (gstin or "").strip().upper()
    if not GSTIN_RE.match(value):
        return [FieldError("gstin", "gstin_format", "GSTIN must be 15 characters in the GST format")]
    errors: list[FieldError] = []
    if check_character(value[:14]) != value[14]:
        errors.append(FieldError("gstin", "gstin_checksum", "The GSTIN check digit is not valid"))
    if state_code is not None and value[:2] != state_code:
        errors.append(
            FieldError("gstin", "gstin_state_mismatch", "The GSTIN state code does not match the selected state")
        )
    if pan is not None and value[2:12] != pan.strip().upper():
        errors.append(FieldError("gstin", "gstin_pan_mismatch", "The GSTIN does not contain the entity's PAN"))
    return errors
