# ruff: noqa: E501
"""What the person changed between the prepared pack and the Submission they sent (Epic 61 A2, FR-307). Pure.

Fields compared: resume kind, pay / bill rate (normalised to 2 dp), units, currency and the e-mail subject. The note is
``unchanged`` (same text), ``edited`` (shares most words) or ``replaced``.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

NOTE_KEEP_RATIO = 0.5


@dataclass(frozen=True, slots=True)
class PackDiff:
    changed_fields: tuple[str, ...]
    note_change: str
    before: dict[str, Any]
    after: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "changed_fields": list(self.changed_fields),
            "note_change": self.note_change,
            "before": self.before,
            "after": self.after,
        }


def _money(value: Any) -> str | None:
    if value is None:
        return None
    return f"{Decimal(str(value)):.2f}"


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).strip().lower()


def note_change(pack_text: str | None, sent_text: str | None) -> str:
    a, b = _norm(pack_text), _norm(sent_text)
    if a == b:
        return "unchanged"
    if not a or not b:
        return "replaced"
    wa, wb = set(a.split()), set(b.split())
    overlap = len(wa & wb) / max(len(wa | wb), 1)
    return "edited" if overlap >= NOTE_KEEP_RATIO else "replaced"


def pack_diff(pack: Any, submission: Any) -> PackDiff:
    """``pack``: a ``SubmissionPack``; ``submission``: a P1 ``Submission`` (single ``rate_unit``)."""
    before = {
        "resume_kind": pack.resume_kind,
        "pay_rate": _money(pack.pay_rate),
        "bill_rate": _money(pack.bill_rate),
        "pay_rate_unit": pack.pay_rate_unit,
        "bill_rate_unit": pack.bill_rate_unit,
        "currency": pack.currency,
        "subject": (pack.email_subject or "").strip() or None,
    }
    unit = getattr(submission, "rate_unit", None)
    after = {
        "resume_kind": submission.resume_kind,
        "pay_rate": _money(submission.pay_rate),
        "bill_rate": _money(submission.bill_rate),
        "pay_rate_unit": unit,
        "bill_rate_unit": unit,
        "currency": submission.currency,
        "subject": (submission.email_subject or "").strip() or None,
    }
    changed: list[str] = []
    for key in ("resume_kind", "pay_rate", "bill_rate", "currency", "subject"):
        if (before[key] is not None and before[key] != after[key]) or (
            before[key] is None and key in ("pay_rate", "bill_rate", "subject") and after[key] is not None
        ):
            changed.append(key)
    for key in ("pay_rate_unit", "bill_rate_unit"):
        if before[key] is not None and before[key] != after[key]:
            changed.append(key)
    note = note_change(pack.summary_text, getattr(submission, "note", None))
    if note != "unchanged":
        changed.append("note")
    return PackDiff(tuple(changed), note, before, after)


__all__ = ["PackDiff", "note_change", "pack_diff"]
