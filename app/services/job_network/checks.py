# ruff: file-ignore[line-too-long]
"""Automated listing checks (Epic 75, spec A5). Deterministic, no LLM. Any finding holds the listing with the
finding codes; the Tenant sees the reasons and excerpts on the admin page.

Also ``redact_contacts`` (strip emails / phones before storing a visitor report's free text) and ``strip_contacts``
helpers. The projection does NOT silently strip contacts from a listing description -- a contact in a listing is a
*check* (``external_contact``), surfaced to the Tenant, never hidden.
"""

import dataclasses
import re

# ---------------------------------------------------------------- lexicons
_FEE_TERMS = (
    "registration fee", "registration charges", "deposit", "security amount", "security deposit",
    "processing charges", "processing fee", "refundable", "pay rs", "pay ₹", "pay inr", "token amount",
    # transliterated Hindi
    "paisa", "paise do", "fees bharna", "registration ke liye", "security jama",
)
_PROHIBITED_TERMS = (
    "escort", "adult services", "mlm", "multi level marketing", "multi-level marketing", "chain marketing",
    "crypto investment", "bitcoin investment", "forex trading guaranteed", "money doubling", "ponzi",
)
# Reuse of the P1/P3 fairness protected-attribute vocabulary (kept self-contained so the network imports nothing from
# candidate / matching packages; ``p5-network-no-candidates``).
_BIASED_TERMS = (
    "male only", "female only", "males only", "females only", "only male", "only female", "boys only",
    "girls only", "unmarried", "married only", "hindu only", "muslim only", "christian only", "upper caste",
    "brahmin only", "fair skinned", "young candidates only", "below 30 only", "under 30 only", "no married women",
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")
_E164_RE = re.compile(r"(?<!\d)\+\d{8,15}(?!\d)")
_HANDLE_RE = re.compile(r"\b(?:wa\.me|t\.me|whatsapp|telegram)\b", re.IGNORECASE)

MIN_DESCRIPTION_CHARS = 200


@dataclasses.dataclass(frozen=True, slots=True)
class CheckFinding:
    code: str
    field: str
    excerpt: str


def _found(text: str, terms: tuple[str, ...]) -> str | None:
    low = text.lower()
    for term in terms:
        idx = low.find(term)
        if idx != -1:
            start = max(0, idx - 20)
            return text[start : idx + len(term) + 20].strip()
    return None


def _external_contact(title: str, description_text: str, verified_domains: frozenset[str]) -> str | None:
    blob = f"{title}\n{description_text}"
    for m in _EMAIL_RE.finditer(blob):
        domain = m.group(0).rsplit("@", 1)[-1].lower()
        if domain not in verified_domains:
            return m.group(0)
    for rx in (_PHONE_RE, _E164_RE):
        m = rx.search(blob)
        if m:
            return m.group(0)
    m = _HANDLE_RE.search(blob)
    return m.group(0) if m else None


def run(
    *,
    title: str,
    description_text: str,
    verified_domains: frozenset[str] = frozenset(),
) -> list[CheckFinding]:
    """Run every deterministic check over one listing's public fields. ``duplicate_spam`` is computed by the sync task
    (it needs the member's other rows) and appended there, not here."""
    findings: list[CheckFinding] = []
    text = description_text or ""

    excerpt = _found(text, _FEE_TERMS) or _found(title, _FEE_TERMS)
    if excerpt:
        findings.append(CheckFinding("fee_request", "description", excerpt))

    contact = _external_contact(title, text, verified_domains)
    if contact:
        findings.append(CheckFinding("external_contact", "description", contact))

    prohibited = _found(text, _PROHIBITED_TERMS) or _found(title, _PROHIBITED_TERMS)
    if prohibited:
        findings.append(CheckFinding("prohibited", "description", prohibited))

    biased = _found(f"{title}\n{text}", _BIASED_TERMS)
    if biased:
        findings.append(CheckFinding("biased_terms", "description", biased))

    if not title.strip() or len(text.strip()) < MIN_DESCRIPTION_CHARS:
        findings.append(CheckFinding("missing_basics", "description", title[:40]))

    return findings


# ---------------------------------------------------------------- report free-text redaction
def redact_contacts(details: str | None) -> str | None:
    """Strip emails / phone numbers from a visitor report's free text before storing (the form warns the reporter not
    to include personal information; this is the backstop)."""
    if not details:
        return details
    out = _EMAIL_RE.sub("[removed]", details)
    out = _PHONE_RE.sub("[removed]", out)
    out = _E164_RE.sub("[removed]", out)
    return out


__all__ = ["MIN_DESCRIPTION_CHARS", "CheckFinding", "redact_contacts", "run"]
