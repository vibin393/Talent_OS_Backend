r"""Sensitive-input guard (Epic 35 A5, NFR-24, AD-35).

The gateway runs ``redact`` on every rendered prompt input (trusted and untrusted) before the model call:

* 12-digit Aadhaar-like numbers (optionally grouped ``4-4-4``) that pass the Verhoeff checksum;
* individual PANs (``[A-Z]{3}P[A-Z]\\d{4}[A-Z]`` -- the 4th letter ``P`` marks a person);
* bank account numbers next to an IFSC code (``ABCD0123456`` and a 9-18 digit number within 40 characters).

Each match becomes ``[REDACTED]``; the counts per kind are stored in ``ai_actions.meta.redactions``.
"""

import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"

_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)  # fmt: skip
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)  # fmt: skip

_AADHAAR_RE = re.compile(r"(?<!\d)([2-9]\d{3})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
_PAN_RE = re.compile(r"\b[A-Z]{3}P[A-Z]\d{4}[A-Z]\b")
_IFSC = r"[A-Z]{4}0[A-Z0-9]{6}"
_ACCOUNT = r"(?<!\d)\d{9,18}(?!\d)"
_BANK_RE = re.compile(rf"(\b{_IFSC}\b)(.{{0,40}}?){_ACCOUNT}|{_ACCOUNT}(.{{0,40}}?)(\b{_IFSC}\b)", re.DOTALL)


def verhoeff_valid(number: str) -> bool:
    if not number.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def _redact_aadhaar(text: str) -> tuple[str, int]:
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        if verhoeff_valid("".join(m.groups())):
            count += 1
            return REDACTED
        return m.group(0)

    return _AADHAAR_RE.sub(sub, text), count


def _redact_bank(text: str) -> tuple[str, int]:
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        if m.group(1):  # IFSC first, account after
            return f"{REDACTED}{m.group(2)}{REDACTED}"
        return f"{REDACTED}{m.group(3)}{REDACTED}"

    return _BANK_RE.sub(sub, text), count


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return ``(redacted text, {kind: replacements})``."""
    counts: dict[str, int] = {}
    text, n = _redact_bank(text)
    if n:
        counts["bank_account"] = n
    text, n = _redact_aadhaar(text)
    if n:
        counts["aadhaar"] = n
    text, n = _PAN_RE.subn(REDACTED, text)
    if n:
        counts["pan"] = n
    return text, counts


def redact_inputs(values: Mapping[str, str]) -> tuple[dict[str, str], dict[str, int]]:
    out: dict[str, str] = {}
    total: dict[str, int] = {}
    for key, value in values.items():
        clean, counts = redact(value) if isinstance(value, str) else (value, {})
        out[key] = clean
        for kind, n in counts.items():
            total[kind] = total.get(kind, 0) + n
    return out, total
