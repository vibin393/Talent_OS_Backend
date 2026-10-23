# ruff: file-ignore[line-too-long]
"""Outreach drafts and the approved send (Epic 56 stories 56.3 / 56.4, FR-287 / FR-288).

* ``validate_draft(text, pay)`` (pure) -- refuses pay amounts outside the published ``min..max`` range
  (``comp_outside_range``; no published range = no amount may be quoted) and protected attributes
  (``protected_term``); appends the tenant opt-out line when missing.
* ``store_draft`` -- the agent's draft for an entry (internal; ``sanitize.outbound`` strips foreign links ->
  flag ``link_removed``). Refused: ``no_ai_consent``, ``channel_suppressed``, ``free_text_channel_unsupported``
  (SMS / WhatsApp outreach is template-only, Known gap 3).
* ``send_approved`` -- runs when a person approves the ``outreach.send`` Proposal: re-validates the (possibly
  edited) text, re-runs ``campaigns.governance.check`` (a ``Suppress`` raises ``ConflictError("suppressed:<reason>")``
  so the Proposal ends ``execution_failed`` with the reason on the card), then queues the email as the approving
  person (``origin='agent'``) with the AI disclosure line and marks the entry ``sent``.

The email is queued with ``category='process'`` (deviation): the P3 marketing delivery gate suppresses any marketing
email without a ``campaign_messages`` row, so the FR-201 decision is taken here under the governance advisory lock,
and the send counts toward the 3-per-7-days cap through the registered cap counter ``sourcing_outreach``.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.settings import get_settings

OPT_OUT_LINE = "If you would rather not hear from us about roles like this, reply STOP or reply 'unsubscribe' and we will not contact you again."
PROTECTED_TERMS: tuple[str, ...] = (
    "age", "aged", "years old", "gender", "male", "female", "woman", "women", "religion",
    "religious", "hindu", "muslim", "christian", "sikh", "jain", "caste", "dalit", "brahmin", "marital", "married",
    "unmarried", "single mother", "pregnan", "maternity", "disability", "disabled", "handicap", "ethnicity", "race",
    "racial", "nationality", "native speaker", "skin colour", "skin color", "sexual orientation", "gay", "lesbian",
    "date of birth",
)  # fmt: skip
_TERM_RES = tuple(
    re.compile(
        rf"(?<![a-z]){re.escape(t.strip())}" + ("" if t.endswith(("pregnan", "handicap")) else r"(?![a-z])"),
        re.IGNORECASE,
    )
    for t in PROTECTED_TERMS
)
_NUM = r"(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)"
_UNIT = r"(lpa|lakhs?|lacs?|l|crores?|cr|k|thousand)?"
_PAY_RE = re.compile(
    rf"(?:(?:₹|rs\.?|inr|\$|usd)\s*{_NUM}\s*{_UNIT}\b)|(?:{_NUM}\s*(lpa|lakhs?|lacs?|crores?|cr)\b)",
    re.IGNORECASE,
)
_MULT = {
    "lpa": Decimal(100_000), "lakh": Decimal(100_000), "lakhs": Decimal(100_000), "lac": Decimal(100_000),
    "lacs": Decimal(100_000), "l": Decimal(100_000), "crore": Decimal(10_000_000), "crores": Decimal(10_000_000),
    "cr": Decimal(10_000_000), "k": Decimal(1000), "thousand": Decimal(1000),
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class PayRange:
    """The published annual range in the Demand's currency (``None`` bounds = not published)."""

    min_annual: Decimal | None
    max_annual: Decimal | None


@dataclass(frozen=True, slots=True)
class DraftCheck:
    text: str
    flags: tuple[str, ...]


def pay_amounts(text: str) -> list[Decimal]:
    """Annual amounts quoted in ``text`` (₹ / Rs / INR / $ prefixed, or followed by LPA / lakh / crore)."""
    out: list[Decimal] = []
    for m in _PAY_RE.finditer(text or ""):
        raw = m.group(1) or m.group(3)
        unit = (m.group(2) or m.group(4) or "").lower()
        try:
            value = Decimal(raw.replace(",", ""))
        except (InvalidOperation, AttributeError):
            continue
        out.append(value * _MULT.get(unit, Decimal(1)))
    return out


def protected_terms(text: str) -> list[str]:
    lowered = f" {text or ''} "
    return [PROTECTED_TERMS[i].strip() for i, rx in enumerate(_TERM_RES) if rx.search(lowered)]


def validate_draft(text: str, pay: PayRange) -> DraftCheck:
    """Raise ``ValidationFailed`` (``comp_outside_range`` / ``protected_term``); return the text with the opt-out
    line appended when it is missing."""
    found = protected_terms(text)
    if found:
        raise ValidationFailed(
            "The draft mentions a protected attribute", code="protected_term", details={"terms": found[:5]}
        )
    for amount in pay_amounts(text):
        low, high = pay.min_annual, pay.max_annual
        if high is None or amount > high or (low is not None and amount < low):
            raise ValidationFailed(
                "The draft quotes pay outside the published range",
                code="comp_outside_range",
                details={"amount": str(amount), "max": str(high) if high is not None else None},
            )
    flags: list[str] = []
    out = text.rstrip()
    if "unsubscribe" not in out.lower() and "reply stop" not in out.lower():
        out = f"{out}\n\n{OPT_OUT_LINE}"
        flags.append("opt_out_added")
    return DraftCheck(out, tuple(flags))


def disclosure(owner_name: str) -> str:
    return get_settings().agent_disclosure_line.format(owner_name=owner_name)


async def pay_range(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> PayRange:
    from app.schemas.matching_profiles import annualise
    from app.services.demands import matching_port

    demand = await matching_port.get_demand(ctx, db, demand_id)
    low = annualise(demand.pay_rate_min, demand.rate_unit)
    high = annualise(demand.pay_rate_max, demand.rate_unit)
    if high is None and low is not None:
        high = low
    return PayRange(low, high)


def sanitize(text: str, max_len: int) -> tuple[str, bool]:
    from app.services.agent_runtime.sanitize import outbound

    clean = outbound(text, max_len=max_len)
    return clean.text, clean.stripped_links > 0


async def send_approved(
    ctx: RequestContext,
    db: AsyncSession,
    entry: Any,
    *,
    channel: str,
    subject: str | None,
    body: str,
    owner_name: str,
    approver_id: uuid.UUID,
    refs: dict[str, Any],
) -> tuple[uuid.UUID | None, str]:
    """Execute an approved outreach send (see module docstring). Returns ``(message_id, status)``."""
    from app.services import comms_service
    from app.services.campaigns import governance

    if channel != "email":
        raise ValidationFailed(
            "Free-text SMS / WhatsApp outreach is not allowed (use an approved template)",
            code="free_text_channel_unsupported",
        )
    pay = await pay_range(ctx, db, entry.demand_id)
    text, _ = sanitize(body, 4000)
    checked = validate_draft(text, pay)
    clean_subject, _ = sanitize(subject or "A role you may be interested in", 200)
    now = datetime.now(UTC)
    address = await governance.address_for(db, entry.candidate_id, channel)
    decision = await governance.check(
        db, candidate_id=entry.candidate_id, channel=channel, address=address, now=now, respect_window=False
    )
    if isinstance(decision, governance.Suppress):
        raise ConflictError(
            f"Not sent: {decision.reason}",
            code=f"suppressed:{decision.reason}"[:50],
            details={"reason": decision.reason},
        )
    if not isinstance(decision, governance.Send):
        raise ConflictError(
            "Marketing sends are paused", code="suppressed:paused", details={"reason": "paused"}
        )
    recipient = await comms_service.recipient_for(db, "candidate", entry.candidate_id)
    html = "".join(f"<p>{escape(line)}</p>" for line in checked.text.splitlines() if line.strip())
    html += f"<p><em>{escape(disclosure(owner_name))}</em></p>"
    result = await comms_service.queue_email(
        ctx, db, origin="agent", to=[recipient], subject=clean_subject, body_html=html, sender_user_id=approver_id,
        related=[("candidate", entry.candidate_id), ("demand", entry.demand_id)], refs=refs, raw=True,
    )  # fmt: skip
    if not result.message_ids or result.warnings.suppressed_addresses:
        raise ConflictError("Not sent: the address is suppressed", code="suppressed:no_address")
    entry.draft_channel, entry.draft_subject, entry.draft_body = (
        channel,
        clean_subject[:200],
        checked.text[:4000],
    )
    entry.draft_status, entry.outreach_sent_at = "sent", now
    await db.flush()
    audit.record(
        "sourcing.outreach_sent", "agent_shortlist_entry", entry.id, session=db,
        candidate_id=str(entry.candidate_id), channel=channel,
    )  # fmt: skip
    return result.message_ids[0], "queued"


async def store_draft(
    ctx: RequestContext, db: AsyncSession, entry: Any, *, channel: str, subject: str | None, body: str
) -> tuple[str, str | None, tuple[str, ...]]:
    """Validate, sanitise and store the agent's draft on ``entry`` (internal only). Returns
    ``(text, subject, flags)``; raises ``ValidationFailed`` with the refusal code."""
    if entry.status != "suggested":
        raise ValidationFailed("The entry is not open for outreach", code="entry_not_open")
    if "no_ai_consent" in (entry.flags or []):
        raise ValidationFailed("No AI-processing consent: draft it yourself", code="no_ai_consent")
    if channel != "email":
        raise ValidationFailed(
            "SMS / WhatsApp outreach must use an approved template", code="free_text_channel_unsupported"
        )
    state = (entry.contactability or {}).get(channel) or {}
    if not state.get("ok"):
        raise ValidationFailed(
            "This channel is suppressed for the candidate", code="channel_suppressed",
            details={"reason": state.get("reason")},
        )  # fmt: skip
    text, stripped = sanitize(body, 4000)
    clean_subject = sanitize(subject, 200)[0] if subject else None
    checked = validate_draft(text, await pay_range(ctx, db, entry.demand_id))
    flags = list(checked.flags)
    if stripped:
        flags.append("link_removed")
        entry.flags = sorted({*(entry.flags or []), "link_removed"})
    entry.draft_channel, entry.draft_subject, entry.draft_body = channel, clean_subject, checked.text[:4000]
    entry.draft_status = "proposed"
    await db.flush()
    return checked.text[:4000], clean_subject, tuple(flags)


__all__ = [
    "OPT_OUT_LINE",
    "PROTECTED_TERMS",
    "DraftCheck",
    "PayRange",
    "disclosure",
    "pay_amounts",
    "pay_range",
    "protected_terms",
    "sanitize",
    "send_approved",
    "store_draft",
    "validate_draft",
]
