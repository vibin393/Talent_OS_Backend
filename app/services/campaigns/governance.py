# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Send governance (Epic 39 A3, FR-201, AD-43): the ONLY module that decides marketing suppression.

``check`` runs immediately before every marketing delivery: in ``sending.send_batch`` and again through the category
gates registered into P1 ``comms_service.deliver`` and the P2 messaging send gate. Order:

1. tenant paused -> ``Defer(None)``; campaign cancelled -> ``Suppress(campaign_cancelled)``; campaign paused /
   enrolment not active / sequence paused -> ``Defer(None)`` (rows re-read ``FOR SHARE``).
2. candidate erased / merged -> ``Suppress(erased)``.
3. consent ``marketing`` (or, only for an ``own_application`` sequence whose enrolment application is live,
   ``engagement``) -> else ``Suppress(no_marketing_consent)``.
4. channel opt-out (email ``email_address_status.marketing_opt_out_at``; sms / whatsapp ``channel_opt_outs``) ->
   ``Suppress(channel_opt_out)``; WhatsApp also needs the P2 ``whatsapp`` consent -> ``Suppress(no_whatsapp_optin)``.
5. address: missing / invalid -> ``no_address``; hard bounce -> ``hard_bounce``; complaint -> ``complaint``.
6. frequency cap: marketing messages handed to delivery (``queued|sent|delivered``) for the candidate in the last 7
   days >= cap -> ``Suppress(frequency_cap)``, under ``pg_advisory_xact_lock(tenant, candidate)``.
7. send window (recipient-local) -> ``Defer(next window start + jitter <= 10 min)``.

``bulk_preview`` evaluates the same rules set-based for an audience preview (quiet hours are not a preview reason).
"""

import dataclasses
import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaigns import Campaign, CampaignMessage, EngagementEvent, Enrollment, MarketingSettings
from app.models.campaigns import Sequence as SequenceModel
from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
from app.models.comms import ChannelOptOut, EmailAddressStatus
from app.models.pipeline import Application, PipelineStage
from app.models.privacy import Consent
from app.services.campaigns import settings as mkt_settings

COUNTED_STATUSES = ("queued", "sent", "delivered")
TERMINAL_STAGE_KEYS = ("hired", "rejected", "withdrawn")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
PREVIEW_REASONS = (
    "no_marketing_consent",
    "channel_opt_out",
    "no_whatsapp_optin",
    "no_address",
    "hard_bounce",
    "complaint",
    "frequency_cap",
)


@dataclasses.dataclass(frozen=True, slots=True)
class Send:
    action: str = "send"
    reason: str | None = None
    until: datetime | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Defer:
    until: datetime | None
    action: str = "defer"
    reason: str | None = "deferred"


@dataclasses.dataclass(frozen=True, slots=True)
class Suppress:
    reason: str
    action: str = "suppress"
    until: datetime | None = None


Decision = Send | Defer | Suppress


# ================================================================ cap counters (Epic 56, coordinator decision C-9)
# Additive registry: other domains (the Sourcing Agent's approved outreach) register a counter so their marketing
# messages count toward the frequency cap. A counter is ``async (db, candidate_ids, since) -> {candidate_id: n}``.
# With no counter registered the cap counts ``campaign_messages`` only (unchanged P3 behaviour).
CapCounter = Callable[[AsyncSession, Sequence[uuid.UUID], datetime], Awaitable[dict[uuid.UUID, int]]]
_cap_counters: dict[str, CapCounter] = {}


def register_cap_counter(key: str, fn: CapCounter) -> None:
    """Register (or replace) a frequency-cap counter (owners call this from ``hooks.py``)."""
    _cap_counters[key] = fn


def cap_counters() -> dict[str, CapCounter]:
    return dict(_cap_counters)


async def _registered_counts(
    db: AsyncSession, candidate_ids: Sequence[uuid.UUID], since: datetime
) -> dict[uuid.UUID, int]:
    totals: dict[uuid.UUID, int] = {}
    for _key, fn in sorted(_cap_counters.items()):
        for cid, n in (await fn(db, candidate_ids, since)).items():
            totals[cid] = totals.get(cid, 0) + int(n)
    return totals


# ================================================================ recipients
async def email_for(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    return (
        await db.execute(
            select(CandidateEmail.email)
            .where(CandidateEmail.candidate_id == candidate_id)
            .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def phone_for(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    return (
        await db.execute(
            select(CandidatePhone.phone_e164)
            .where(CandidatePhone.candidate_id == candidate_id)
            .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def address_for(db: AsyncSession, candidate_id: uuid.UUID, channel: str) -> str | None:
    value = await email_for(db, candidate_id) if channel == "email" else await phone_for(db, candidate_id)
    return str(value) if value else None


def valid_address(channel: str, address: str | None) -> bool:
    if not address:
        return False
    return bool(EMAIL_RE.match(address)) if channel == "email" else bool(E164_RE.match(address))


def jitter_seconds(seed: str) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 600


def next_window(now: datetime, tz: Any, start: Any, end: Any, seed: str) -> datetime | None:
    """None when ``now`` is inside ``[start, end)`` local time, else the next window start + jitter (UTC)."""
    local = now.astimezone(tz)
    if start <= local.time() < end:
        return None
    day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    at = datetime.combine(day, start, tzinfo=tz) + timedelta(seconds=jitter_seconds(seed))
    return at.astimezone(now.tzinfo) if now.tzinfo else at


async def window_defer(
    db: AsyncSession, row: MarketingSettings, candidate_id: uuid.UUID, channel: str, now: datetime, seed: str
) -> datetime | None:
    location = (
        await db.execute(select(Candidate.location).where(Candidate.id == candidate_id))
    ).scalar_one_or_none()
    country = (location or {}).get("country") if isinstance(location, dict) else None
    tz = mkt_settings.zone_for_country(country, row.default_timezone)
    start, end = mkt_settings.effective_windows(row)[channel]
    return next_window(now, tz, start, end, seed)


async def _live_application(db: AsyncSession, application_id: uuid.UUID | None) -> bool:
    if application_id is None:
        return False
    key = (
        await db.execute(
            select(PipelineStage.key)
            .join(Application, Application.stage_id == PipelineStage.id)
            .where(Application.id == application_id, Application.archived_at.is_(None))
        )
    ).scalar_one_or_none()
    return key is not None and key not in TERMINAL_STAGE_KEYS


async def _has_consent(db: AsyncSession, candidate_id: uuid.UUID, purpose: str) -> bool:
    from app.services.privacy import consent

    return await consent.has_active_consent(db, candidate_id, purpose)


async def _owner_state(
    db: AsyncSession, message: CampaignMessage
) -> tuple[Decision | None, SequenceModel | None, Enrollment | None]:
    if message.campaign_id is not None:
        campaign = (
            await db.execute(
                select(Campaign).where(Campaign.id == message.campaign_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if campaign is None or campaign.status == "cancelled":
            return Suppress("campaign_cancelled"), None, None
        if campaign.status != "sending":
            return Defer(None), None, None
        return None, None, None
    enrollment = (
        await db.execute(
            select(Enrollment).where(Enrollment.id == message.enrollment_id).with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if enrollment is None:
        return Suppress("enrollment_ended"), None, None
    if enrollment.status in ("exited", "suppressed"):
        return Suppress("enrollment_ended"), None, enrollment
    sequence = await db.get(SequenceModel, enrollment.sequence_id)
    # ``completed`` = the last step was handed over in this very run: its message still goes out
    if enrollment.status == "paused" or sequence is None or sequence.status != "active":
        return Defer(None), sequence, enrollment
    return None, sequence, enrollment


async def _frequency_count(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, now: datetime, exclude: uuid.UUID | None
) -> int:
    key = int(hashlib.sha256(f"mkt-cap:{tenant_id}:{candidate_id}".encode()).hexdigest()[:15], 16)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    stmt = (
        select(func.count())
        .select_from(CampaignMessage)
        .where(
            CampaignMessage.candidate_id == candidate_id,
            CampaignMessage.status.in_(COUNTED_STATUSES),
            CampaignMessage.sent_at > now - timedelta(days=mkt_settings.CAP_DAYS),
        )
    )
    if exclude is not None:
        stmt = stmt.where(CampaignMessage.id != exclude)
    count = int((await db.execute(stmt)).scalar_one())
    if _cap_counters:
        extra = await _registered_counts(db, [candidate_id], now - timedelta(days=mkt_settings.CAP_DAYS))
        count += extra.get(candidate_id, 0)
    return count


async def check(
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    channel: str,
    address: str | None,
    now: datetime,
    message: CampaignMessage | None = None,
    respect_window: bool = True,
) -> Decision:
    """The single marketing suppression decision (see module docstring for the order)."""
    row = await mkt_settings.get_row(db)
    tenant_id = row.tenant_id
    # 1. pause / cancel / enrolment state
    if row.paused_at is not None:
        return Defer(None)
    sequence: SequenceModel | None = None
    enrollment: Enrollment | None = None
    if message is not None:
        decided, sequence, enrollment = await _owner_state(db, message)
        if decided is not None:
            return decided
    # 2. erased
    cand = (
        await db.execute(
            select(Candidate.erased_at, Candidate.merged_into_id).where(Candidate.id == candidate_id)
        )
    ).first()
    if cand is None or cand[0] is not None or cand[1] is not None:
        return Suppress("erased")
    # 3. consent
    if not await _has_consent(db, candidate_id, "marketing"):
        allowed = (
            sequence is not None
            and sequence.basis == "own_application"
            and enrollment is not None
            and await _live_application(db, enrollment.application_id)
            and await _has_consent(db, candidate_id, "engagement")
        )
        if not allowed:
            return Suppress("no_marketing_consent")
    # 4. channel opt-out
    status: EmailAddressStatus | None = None
    if channel == "email":
        if address:
            status = await db.get(EmailAddressStatus, (tenant_id, address.strip().lower()))
        if status is not None and status.marketing_opt_out_at is not None:
            return Suppress("channel_opt_out")
    else:
        from app.services.messaging import opt_outs

        if address and await opt_outs.is_opted_out(db, channel, address):
            return Suppress("channel_opt_out")
        if channel == "whatsapp":
            from app.services.messaging.sending import has_whatsapp_consent

            if not await has_whatsapp_consent(db, "candidate", candidate_id):
                return Suppress("no_whatsapp_optin")
    # 5. address
    if not valid_address(channel, address):
        return Suppress("no_address")
    if status is not None and status.hard_bounced_at is not None:
        return Suppress("hard_bounce")
    complained = (
        await db.execute(
            select(EngagementEvent.id)
            .where(EngagementEvent.candidate_id == candidate_id, EngagementEvent.kind == "complained")
            .limit(1)
        )
    ).first()
    if complained is not None:
        return Suppress("complaint")
    # 6. frequency cap (advisory lock serialises concurrent sends to one candidate)
    sent = await _frequency_count(
        db, tenant_id, candidate_id, now, message.id if message is not None else None
    )
    if sent >= row.frequency_cap_count:
        return Suppress("frequency_cap")
    # 7. send window
    if respect_window:
        seed = str(message.id) if message is not None else str(candidate_id)
        eta = await window_defer(db, row, candidate_id, channel, now, seed)
        if eta is not None:
            return Defer(eta)
    return Send()


# ================================================================ bulk preview (set-based)
async def _first_addresses(
    db: AsyncSession, model: Any, column: Any, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    ranked = (
        select(
            model.candidate_id.label("cid"),
            column.label("addr"),
            func.row_number()
            .over(partition_by=model.candidate_id, order_by=(model.is_primary.desc(), model.created_at))
            .label("rn"),
        )
        .where(model.candidate_id.in_(ids))
        .subquery()
    )
    rows = await db.execute(select(ranked.c.cid, ranked.c.addr).where(ranked.c.rn == 1))
    return {cid: str(addr) for cid, addr in rows.all() if addr}


async def _ids_with(db: AsyncSession, stmt: Any) -> set[uuid.UUID]:
    return set((await db.execute(stmt)).scalars())


def _chunks(ids: Sequence[uuid.UUID], size: int = 20_000) -> Iterable[Sequence[uuid.UUID]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


async def bulk_preview(
    db: AsyncSession, candidate_ids: Iterable[uuid.UUID], channels: Sequence[str], now: datetime
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """``(reachable, suppressed_by_reason)`` per channel, evaluated with bulk queries over the same rules."""
    from app.services.messaging.opt_outs import phone_hash

    ids = sorted(set(candidate_ids))
    row = await mkt_settings.get_row(db)
    since = now - timedelta(days=mkt_settings.CAP_DAYS)
    consent_m: set[uuid.UUID] = set()
    consent_w: set[uuid.UUID] = set()
    complained: set[uuid.UUID] = set()
    capped: set[uuid.UUID] = set()
    erased: set[uuid.UUID] = set()
    emails: dict[uuid.UUID, str] = {}
    phones: dict[uuid.UUID, str] = {}
    for chunk in _chunks(ids):
        granted = await db.execute(
            select(Consent.candidate_id, Consent.purpose).where(
                Consent.candidate_id.in_(chunk),
                Consent.status == "granted",
                Consent.purpose.in_(("marketing", "whatsapp")),
            )
        )
        for cid, purpose in granted.all():
            (consent_m if purpose == "marketing" else consent_w).add(cid)
        complained |= await _ids_with(
            db,
            select(EngagementEvent.candidate_id).where(
                EngagementEvent.candidate_id.in_(chunk), EngagementEvent.kind == "complained"
            ),
        )
        counts = await db.execute(
            select(CampaignMessage.candidate_id, func.count())
            .where(
                CampaignMessage.candidate_id.in_(chunk),
                CampaignMessage.status.in_(COUNTED_STATUSES),
                CampaignMessage.sent_at > since,
            )
            .group_by(CampaignMessage.candidate_id)
        )
        sent_counts = {cid: int(n) for cid, n in counts.all()}
        if _cap_counters:
            for cid, n in (await _registered_counts(db, chunk, since)).items():
                sent_counts[cid] = sent_counts.get(cid, 0) + n
        capped |= {cid for cid, n in sent_counts.items() if n >= row.frequency_cap_count}
        erased |= await _ids_with(
            db,
            select(Candidate.id).where(
                Candidate.id.in_(chunk),
                (Candidate.erased_at.is_not(None)) | (Candidate.merged_into_id.is_not(None)),
            ),
        )
        if "email" in channels:
            emails |= await _first_addresses(db, CandidateEmail, CandidateEmail.email, chunk)
        if "sms" in channels or "whatsapp" in channels:
            phones |= await _first_addresses(db, CandidatePhone, CandidatePhone.phone_e164, chunk)
    statuses: dict[str, EmailAddressStatus] = {}
    addr_list = sorted({a.lower() for a in emails.values()})
    for i in range(0, len(addr_list), 20_000):
        part = addr_list[i : i + 20_000]
        for s in (
            await db.execute(select(EmailAddressStatus).where(EmailAddressStatus.address.in_(part)))
        ).scalars():
            statuses[str(s.address).lower()] = s
    opted: dict[str, set[str]] = {}
    for ch in ("sms", "whatsapp"):
        if ch in channels:
            opted[ch] = set(
                (
                    await db.execute(
                        select(ChannelOptOut.phone_hash).where(
                            ChannelOptOut.channel == ch, ChannelOptOut.revoked_at.is_(None)
                        )
                    )
                ).scalars()
            )
    hashes = {cid: phone_hash(p) for cid, p in phones.items()} if opted else {}

    reachable: dict[str, int] = {}
    suppressed: dict[str, dict[str, int]] = {}
    for ch in channels:
        counter: dict[str, int] = {}
        ok = 0
        for cid in ids:
            reason: str | None = None
            if cid in erased:
                reason = "no_address"
            elif cid not in consent_m:
                reason = "no_marketing_consent"
            elif ch == "email":
                addr = emails.get(cid)
                st = statuses.get(addr.lower()) if addr else None
                if st is not None and st.marketing_opt_out_at is not None:
                    reason = "channel_opt_out"
                elif not valid_address("email", addr):
                    reason = "no_address"
                elif st is not None and st.hard_bounced_at is not None:
                    reason = "hard_bounce"
            else:
                phone = phones.get(cid)
                if phone and hashes.get(cid) in opted.get(ch, set()):
                    reason = "channel_opt_out"
                elif ch == "whatsapp" and cid not in consent_w:
                    reason = "no_whatsapp_optin"
                elif not valid_address(ch, phone):
                    reason = "no_address"
            if reason is None and cid in complained:
                reason = "complaint"
            if reason is None and cid in capped:
                reason = "frequency_cap"
            if reason is None:
                ok += 1
            else:
                counter[reason] = counter.get(reason, 0) + 1
        reachable[ch] = ok
        suppressed[ch] = counter
    return reachable, suppressed
