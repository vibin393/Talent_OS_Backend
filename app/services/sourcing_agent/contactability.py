# ruff: file-ignore[line-too-long]
"""Per-channel contactability (Epic 56, FR-288). The SAME rules as the P3 marketing governance
(``campaigns.governance.bulk_preview``) are evaluated **before** drafting; suppressed Candidates get no draft, and the
send re-runs ``governance.check`` at execution.

* ``contactability(db, candidate_ids, preferred)`` -- ``{candidate_id: Contactability}``; the reason of a suppressed
  channel is the governance reason (``no_marketing_consent``, ``channel_opt_out``, ``no_whatsapp_optin``,
  ``no_address``, ``hard_bounce``, ``complaint``, ``frequency_cap``).
* ``best_channel`` -- the tenant's ``preferred_channel`` if it is ok, else the first ok of email, WhatsApp, SMS.
* ``do_not_contact_ids`` -- Known gap 1: Talent OS has no Do-Not-Contact field; Phase 4 reads it as "marketing
  consent withdrawn **and** every channel opted out (or without an address)".
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comms import EmailAddressStatus
from app.models.privacy import Consent
from app.schemas.sourcing_agent import ChannelState, Contactability

CHANNELS: tuple[str, ...] = ("email", "sms", "whatsapp")
FALLBACK_ORDER: tuple[str, ...] = ("email", "whatsapp", "sms")


def best_channel(states: dict[str, ChannelState], preferred: str | None) -> str | None:
    if preferred and states.get(preferred) is not None and states[preferred].ok:
        return preferred
    return next((ch for ch in FALLBACK_ORDER if states.get(ch) is not None and states[ch].ok), None)


async def contactability(
    db: AsyncSession,
    candidate_ids: Iterable[uuid.UUID],
    *,
    preferred: str | None = "email",
    now: datetime | None = None,
) -> dict[uuid.UUID, Contactability]:
    from app.services.campaigns import governance

    at = now or datetime.now(UTC)
    out: dict[uuid.UUID, Contactability] = {}
    for cid in dict.fromkeys(candidate_ids):
        reachable, suppressed = await governance.bulk_preview(db, [cid], CHANNELS, at)
        states: dict[str, ChannelState] = {}
        for ch in CHANNELS:
            if reachable.get(ch, 0) > 0:
                states[ch] = ChannelState(ok=True)
            else:
                reasons = sorted((suppressed.get(ch) or {}).keys())
                states[ch] = ChannelState(ok=False, reason=reasons[0] if reasons else "no_address")
        out[cid] = Contactability(
            email=states["email"],
            sms=states["sms"],
            whatsapp=states["whatsapp"],
            best_channel=best_channel(states, preferred),  # type: ignore[arg-type]
        )
    return out


def as_json(c: Contactability) -> dict[str, dict[str, object]]:
    return {ch: getattr(c, ch).model_dump() for ch in CHANNELS}


def suppressed_counts(items: Iterable[Contactability]) -> dict[str, int]:
    """Candidates with no usable channel, by the reason of their best-ranked channel (email first)."""
    counts: dict[str, int] = {}
    for c in items:
        if c.best_channel is None:
            reason = c.email.reason or c.whatsapp.reason or c.sms.reason or "no_address"
            counts[reason] = counts.get(reason, 0) + 1
    return counts


async def do_not_contact_ids(db: AsyncSession, candidate_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Marketing consent withdrawn (and none granted) AND every channel opted out or without an address."""
    from app.services.campaigns import governance
    from app.services.messaging import opt_outs

    if not candidate_ids:
        return set()
    rows = (
        await db.execute(
            select(Consent.candidate_id, Consent.status).where(
                Consent.candidate_id.in_(list(candidate_ids)), Consent.purpose == "marketing"
            )
        )
    ).all()
    granted = {cid for cid, status in rows if status == "granted"}
    withdrawn = {cid for cid, status in rows if status == "withdrawn"} - granted
    out: set[uuid.UUID] = set()
    for cid in sorted(withdrawn, key=str):
        email = await governance.email_for(db, cid)
        email_blocked = True
        if email:
            status = await db.get(EmailAddressStatus, (await _tenant_of(db), str(email).strip().lower()))
            email_blocked = status is not None and status.marketing_opt_out_at is not None
        phone = await governance.phone_for(db, cid)
        sms_blocked = not phone or await opt_outs.is_opted_out(db, "sms", phone)
        wa_blocked = not phone or await opt_outs.is_opted_out(db, "whatsapp", phone)
        if email_blocked and sms_blocked and wa_blocked:
            out.add(cid)
    return out


async def _tenant_of(db: AsyncSession) -> uuid.UUID:
    from app.services import entity_refs

    return entity_refs.session_tenant(db)


__all__ = ["CHANNELS", "as_json", "best_channel", "contactability", "do_not_contact_ids", "suppressed_counts"]
