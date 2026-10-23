# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Self-scheduling offers, picks and counter-proposals (Epic 59 stories 59.2 / 59.3; FR-298, NFR-57).

* ``send`` -- a ``draft`` offer becomes ``open``: older offers are superseded, a Secure Link (purpose ``slot_pick``,
  subject ``scheduling_slot_offer``, expiry ``link_days``) is issued and the templated message goes out (email: the
  platform's approved "pick a time" text + the link; WhatsApp: the tenant's approved template; portal: a Candidate Portal
  notification). Times are rendered in the Candidate's zone.
* ``pick`` -- the Candidate picks a slot (Secure Link or portal): idempotent on ``pick_id`` (a duplicate pick returns
  the same state, ``deduplicated``); emits the foundation ``SlotPicked`` and hands the pick to the agent.
* ``counter`` -- "None of these work": up to 3 windows + an optional note (untrusted, PII). ``SlotPicked`` is not
  emitted; the agent resumes / continues with ``counter_proposed``.

A superseded offer's link keeps working while its request is open and shows the newest offer ("That time was just
taken. Here are new options."). Links are revoked when the request closes (booked, cancelled, expired, handed off).
"""

import dataclasses
import uuid
from datetime import datetime, timedelta
from html import escape
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.events import emit
from app.core.settings import get_settings
from app.events.scheduling import SlotPicked
from app.events.scheduling_agent import SchedulingRunNeeded
from app.models.scheduling_agent import OPEN_REQUEST_STATUSES, SchedulingRequest, SchedulingSlotOffer
from app.schemas.scheduling_public import CounterWindowIn, PublicSlot, SlotPickView
from app.services import entity_refs
from app.services.scheduling_agent import history, requests
from app.services.scheduling_agent.common import (
    LINK_MAX_USES,
    LINK_PURPOSE,
    LINK_SUBJECT,
    actor_of,
    conflict,
    invalid,
    now,
    request_row,
    settings_row,
)

MODE_LABELS = {"video": "Video call", "phone": "Phone", "in_person": "In person"}
MAX_COUNTER_DAYS = 60


@dataclasses.dataclass(frozen=True, slots=True)
class Subject:
    tenant_id: uuid.UUID
    offer_id: uuid.UUID
    candidate_id: uuid.UUID | None
    via: str  # link | portal


# ================================================================ helpers
def _slot_label(start: datetime, end: datetime, tz: str) -> str:
    from app.services.interviews import timeutil

    zone = timeutil.safe_zone(tz)
    a, b = start.astimezone(zone), end.astimezone(zone)
    return f"{a.strftime('%a %d %b, %I:%M %p').replace(' 0', ' ')} - {b.strftime('%I:%M %p').lstrip('0')}"


async def _context(db: AsyncSession, req: SchedulingRequest) -> dict[str, Any]:
    from app.models.demands import Demand
    from app.models.interviews import InterviewStage

    tenant = await entity_refs.tenant(db, req.tenant_id)
    demand = await db.get(Demand, req.demand_id)
    stage = await db.get(InterviewStage, req.stage_id)
    owner = await entity_refs.user(db, req.owner_user_id)
    who = await entity_refs.addressee(db, "candidate", req.candidate_id)
    return {
        "tenant": tenant.name, "demand_title": demand.title if demand is not None else "the role",
        "stage_name": stage.name if stage is not None else "interview", "owner_name": owner.name if owner else tenant.name,
        "candidate_first": entity_refs.first_name(who.full_name if who else None) or "there",
    }  # fmt: skip


def _disclosure(name: str) -> str:
    return get_settings().agent_disclosure_line.format(owner_name=name)


async def offer_message(
    db: AsyncSession, req: SchedulingRequest, offer: SchedulingSlotOffer, link: str
) -> tuple[str, str]:
    """The platform's approved "pick a time" text (subject, HTML): merge values + the offered times + the link."""
    data = await _context(db, req)
    times = "".join(
        f"<li>{escape(_slot_label(datetime.fromisoformat(s['start']), datetime.fromisoformat(s['end']), req.candidate_timezone))}</li>"
        for s in offer.slots or []
    )
    subject = f"Pick a time for your {data['stage_name']} interview"
    body = (
        f"<p>Hello {escape(data['candidate_first'])},</p>"
        f"<p>{escape(data['tenant'])} would like to schedule your {escape(data['stage_name'])} interview for "
        f"{escape(data['demand_title'])} ({int(req.duration_minutes)} min, {escape(MODE_LABELS.get(req.mode or 'video', 'Video call'))}).</p>"
        f"<p>These times are shown in {escape(req.candidate_timezone)}:</p><ul>{times}</ul>"
        f'<p><a href="{escape(link)}">Pick a time</a> - or tell us what works better.</p>'
        f"<p><em>{escape(_disclosure(data['owner_name']))}</em></p>"
    )
    return subject, body


async def _email(
    ctx: RequestContext, db: AsyncSession, req: SchedulingRequest, subject: str, html: str
) -> uuid.UUID | None:
    from app.services import comms_service

    try:
        recipient = await comms_service.recipient_for(db, "candidate", req.candidate_id)
    except Exception:
        return None
    result = await comms_service.queue_email(
        ctx, db, origin="agent" if getattr(ctx, "actor_kind", None) == "agent" else "system", to=[recipient],
        subject=subject, body_html=html, related=[("candidate", req.candidate_id), ("application", req.application_id)],
        refs={"candidate_id": req.candidate_id, "demand_id": req.demand_id}, reply_to_user_id=req.owner_user_id, raw=True,
    )  # fmt: skip
    return result.message_ids[0] if result.message_ids else None


async def _whatsapp(ctx: RequestContext, db: AsyncSession, req: SchedulingRequest) -> uuid.UUID | None:
    from app.schemas.messaging import RecipientRef
    from app.services.messaging import sending

    settings = await settings_row(db)
    if settings.whatsapp_template_id is None:
        raise conflict(
            "channel_unavailable", "No approved WhatsApp template is set for scheduling", channel="whatsapp"
        )
    try:
        async with db.begin_nested():
            result = await sending.send(
                ctx, db, "whatsapp", settings.whatsapp_template_id,
                [RecipientRef(entity_type="candidate", entity_id=req.candidate_id)],
                {"candidate_id": str(req.candidate_id), "demand_id": str(req.demand_id)}, origin="agent",
            )  # fmt: skip
    except Exception:
        logger.info("Scheduling {}: WhatsApp offer not sent", req.id)
        return None
    first = result.results[0] if result.results else None
    return first.message_id if first is not None else None


async def _portal_notice(db: AsyncSession, req: SchedulingRequest, offer: SchedulingSlotOffer) -> None:
    from app.services import notifications_service as ns

    try:
        await ns.fan_out(db, "scheduling_pick_time", {
            "candidate_id": str(req.candidate_id), "offer_id": str(offer.id), "request_id": str(req.id),
            "event_id": f"sched_offer:{offer.id}",
        })  # fmt: skip
    except Exception:  # pragma: no cover - notifications never block the offer
        logger.info("Scheduling {}: portal notice not delivered", req.id)


def pick_url(token: str) -> str:
    return secure_links.link_url(token)


# ================================================================ send
async def send(
    ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID, channel: str, *, partial_ok: bool = False
) -> dict[str, Any]:
    """Open a draft offer and send it. 409: ``offer_not_draft``, ``use_offer_partial``, ``no_slots``,
    ``scheduling_request_closed``, ``max_rounds_reached``, ``channel_unavailable``."""
    offer = await db.get(SchedulingSlotOffer, offer_id)
    if offer is None:
        raise NotFound("Offer not found")
    req = await request_row(db, offer.request_id, for_update=True)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise conflict(
            "scheduling_request_closed", "The scheduling request is no longer open", status=req.status
        )
    if offer.status != "draft":
        raise conflict("offer_not_draft", "Only a draft offer can be sent", status=offer.status)
    if offer.partial and not partial_ok:
        raise conflict("use_offer_partial", "Partial options need the recruiter (scheduling.offer_partial)")
    if not offer.slots:
        raise conflict("no_slots", "The offer has no slots")
    settings = await settings_row(db)
    if offer.round > settings.max_rounds:
        raise conflict("max_rounds_reached", "The negotiation reached its maximum number of rounds")
    if channel not in ("email", "whatsapp", "sms", "portal"):
        raise invalid("invalid_channel", "Unknown channel")
    if channel == "sms":
        raise conflict(
            "channel_unavailable",
            "SMS offers need a DLT template; use email, WhatsApp or the portal",
            channel="sms",
        )
    at = now()
    for other in await requests.offers_of(db, req.id):
        if other.id != offer.id and other.status in ("open", "picked", "draft"):
            other.status, other.picked_index = (
                "superseded",
                None,
            )  # pick_id stays (idempotency, "slot taken" notice)
    await db.flush()
    expires = at + timedelta(days=int(settings.link_days))
    link, token = await secure_links.issue(
        db, tenant_id=req.tenant_id, purpose=LINK_PURPOSE, subject_type=LINK_SUBJECT, subject_id=offer.id,
        ttl=expires - at, max_uses=LINK_MAX_USES, created_by=req.owner_user_id,
        metadata={"candidate_id": str(req.candidate_id)},
    )  # fmt: skip
    offer.status, offer.channel, offer.sent_at, offer.expires_at, offer.secure_link_id = (
        "open",
        channel,
        at,
        expires,
        link.id,
    )
    offer.run_id = getattr(ctx, "agent_run_id", None) or offer.run_id
    req.round = offer.round
    history.set_status(db, req, "slots_offered")
    await db.flush()
    url = pick_url(token)
    message_id: uuid.UUID | None = None
    if channel == "whatsapp":
        message_id = await _whatsapp(ctx, db, req)
    if channel == "portal":
        await _portal_notice(db, req, offer)
    subject, body = await offer_message(
        db, req, offer, url
    )  # the link always goes by email too (when on record)
    email_id = await _email(ctx, db, req, subject, body)
    message_id = message_id or email_id
    kind, actor_id = actor_of(ctx)
    history.record(
        db, req, "offered", actor_kind=kind, actor_id=actor_id,
        detail={"offer_id": offer.id, "round": offer.round, "channel": channel, "slots": len(offer.slots or []),
                "partial": offer.partial, "message_id": message_id},
    )  # fmt: skip
    await db.flush()
    return {"message_id": message_id, "link_expires_at": expires, "link_url": url}


# ================================================================ candidate side
async def link_subject(token: str) -> Subject:
    link = await secure_links.resolve(token, (LINK_PURPOSE,))
    if link.subject_type != LINK_SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    raw = link.meta.get("candidate_id")
    return Subject(link.tenant_id, link.subject_id, uuid.UUID(str(raw)) if raw else None, "link")


def portal_subject(tenant_id: uuid.UUID, candidate_id: uuid.UUID, offer_id: uuid.UUID) -> Subject:
    return Subject(tenant_id, offer_id, candidate_id, "portal")


async def _load(
    db: AsyncSession, subject: Subject, *, lock: bool = False
) -> tuple[SchedulingSlotOffer, SchedulingRequest]:
    stmt = select(SchedulingSlotOffer).where(SchedulingSlotOffer.id == subject.offer_id)
    if lock:
        stmt = stmt.with_for_update()
    offer = (await db.execute(stmt)).scalar_one_or_none()
    if offer is None or (subject.candidate_id is not None and offer.candidate_id != subject.candidate_id):
        raise NotFound("Offer not found")
    req = await request_row(db, offer.request_id, for_update=lock)
    return offer, req


async def _current(
    db: AsyncSession, offer: SchedulingSlotOffer, req: SchedulingRequest
) -> tuple[SchedulingSlotOffer, str | None]:
    """The offer the Candidate should see: a superseded offer shows the newest open one of the open request."""
    if offer.status in ("superseded", "picked", "declined") and req.status in OPEN_REQUEST_STATUSES:
        newest = await requests.latest_offer(db, req.id, "open")
        if newest is not None and newest.id != offer.id and newest.round > offer.round:
            notice = "slot_taken" if offer.pick_id is not None else "new_options"
            return newest, notice
    return offer, None


def _status(offer: SchedulingSlotOffer, req: SchedulingRequest) -> str:
    if req.status == "booked":
        return "booked"
    if req.status in ("cancelled", "handed_off"):
        return "closed"
    if req.status == "expired" or offer.status == "expired":
        return "expired"
    if offer.status == "open":
        return "open"
    if offer.status == "picked":
        return "picked"
    if offer.status == "declined":
        return "counter_proposed"
    return "closed"


async def _view(
    db: AsyncSession, offer: SchedulingSlotOffer, req: SchedulingRequest, notice: str | None, *, portal: bool
) -> SlotPickView:
    from app.models.interviews import Interview

    data = await _context(db, req)
    booked_start = None
    if req.booked_interview_id:
        interview = await db.get(Interview, req.booked_interview_id)
        booked_start = interview.starts_at if interview is not None else None
    status = _status(offer, req)
    slots = [
        PublicSlot(index=i, start=datetime.fromisoformat(s["start"]), end=datetime.fromisoformat(s["end"]))
        for i, s in enumerate(offer.slots or [])
    ] if status == "open" else []  # fmt: skip
    view = SlotPickView(
        offer_id=offer.id if portal else None, tenant=data["tenant"], demand_title=data["demand_title"],
        stage_name=data["stage_name"], duration=int(req.duration_minutes), mode=req.mode, slots=slots,
        candidate_timezone=req.candidate_timezone, status=status, expires_at=offer.expires_at,
        booked_start=booked_start, picked_index=offer.picked_index, notice=notice,
    )  # fmt: skip
    return view


async def view(subject: Subject, *, portal: bool = False) -> SlotPickView:
    async with tenant_session(subject.tenant_id) as db:
        offer, req = await _load(db, subject)
        current, notice = await _current(db, offer, req)
        return await _view(db, current, req, notice, portal=portal)


async def pick(subject: Subject, index: int, pick_id: uuid.UUID) -> tuple[SlotPickView, bool]:
    """Record the pick (idempotent on ``pick_id``). Returns ``(view, deduplicated)``. 409 ``offer_not_open`` /
    ``invalid_slot``."""
    async with tenant_session(subject.tenant_id) as db:
        offer, req = await _load(db, subject, lock=True)
        offer, _ = await _current(db, offer, req)
        dup = (
            await db.execute(select(SchedulingSlotOffer).where(SchedulingSlotOffer.pick_id == pick_id))
        ).scalar_one_or_none()
        if dup is not None:
            return await _view(db, dup, req, None, portal=subject.via == "portal"), True
        if offer.status == "picked" and offer.picked_index == index:
            return await _view(db, offer, req, None, portal=subject.via == "portal"), True
        if offer.status != "open" or req.status != "slots_offered":
            raise conflict("offer_not_open", "These times are no longer open", status=_status(offer, req))
        if offer.expires_at is not None and offer.expires_at <= now():
            raise conflict("offer_not_open", "This link has expired", status="expired")
        if not 0 <= index < len(offer.slots or []):
            raise invalid("invalid_slot", "Pick one of the offered times")
        slot = offer.slots[index]
        at = now()
        offer.status, offer.picked_index, offer.picked_at, offer.pick_id = "picked", index, at, pick_id
        history.set_status(db, req, "picked")
        history.record(
            db,
            req,
            "picked",
            actor_kind="candidate",
            actor_id=req.candidate_id,
            detail={"offer_id": offer.id, "index": index, "start": slot["start"]},
        )
        await db.flush()
        emit(db, SlotPicked(
            tenant_id=req.tenant_id, proposal_ref=offer.id, interview_id=req.interview_id, application_id=req.application_id,
            candidate_id=req.candidate_id, slot_start=datetime.fromisoformat(slot["start"]),
            slot_end=datetime.fromisoformat(slot["end"]), run_id=offer.run_id,
        ))  # fmt: skip
        emit(
            db,
            SchedulingRunNeeded(
                tenant_id=req.tenant_id, request_id=req.id, trigger_key="slot_picked", offer_id=offer.id
            ),
        )
        out = await _view(db, offer, req, None, portal=subject.via == "portal")
    return out, False


def _validate_windows(windows: list[CounterWindowIn], duration: int) -> list[dict[str, str]]:
    at = now()
    out: list[dict[str, str]] = []
    for w in windows:
        if w.start.tzinfo is None or w.end.tzinfo is None:
            raise invalid("invalid_window", "Times need a time zone")
        if w.end <= w.start or (w.end - w.start) < timedelta(minutes=duration):
            raise invalid("invalid_window", f"Each time range must be at least {duration} minutes")
        if w.start <= at:
            raise invalid("invalid_window", "Suggest times in the future")
        if w.start > at + timedelta(days=MAX_COUNTER_DAYS):
            raise invalid("invalid_window", "Suggest times within the next 60 days")
        out.append({"start": w.start.isoformat(), "end": w.end.isoformat()})
    return out


async def counter(subject: Subject, windows: list[CounterWindowIn], note: str | None) -> SlotPickView:
    """ "None of these work": the offer is declined with up to 3 windows (+ note); the agent continues."""
    async with tenant_session(subject.tenant_id) as db:
        offer, req = await _load(db, subject, lock=True)
        offer, _ = await _current(db, offer, req)
        if offer.status != "open" or req.status != "slots_offered":
            raise conflict("offer_not_open", "These times are no longer open", status=_status(offer, req))
        offer.status = "declined"
        offer.counter_windows = _validate_windows(windows, int(req.duration_minutes))
        offer.candidate_note = (note or "").strip()[:500] or None
        history.set_status(db, req, "counter_proposed")
        history.record(
            db,
            req,
            "counter_proposed",
            actor_kind="candidate",
            actor_id=req.candidate_id,
            detail={"offer_id": offer.id, "windows": len(windows), "has_note": bool(offer.candidate_note)},
        )
        await db.flush()
        emit(
            db,
            SchedulingRunNeeded(
                tenant_id=req.tenant_id, request_id=req.id, trigger_key="counter_proposed", offer_id=offer.id
            ),
        )
        return await _view(db, offer, req, None, portal=subject.via == "portal")


async def set_timezone(subject: Subject, tz: str) -> SlotPickView:
    from app.services.interviews import timeutil

    timeutil.zone(tz)  # 422 invalid_timezone
    async with tenant_session(subject.tenant_id) as db:
        offer, req = await _load(db, subject, lock=True)
        if req.status in OPEN_REQUEST_STATUSES:
            req.candidate_timezone = tz
            await db.flush()
        current, notice = await _current(db, offer, req)
        return await _view(db, current, req, notice, portal=subject.via == "portal")


async def portal_list(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> list[SlotPickView]:
    async with tenant_session(tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(SchedulingSlotOffer)
                    .join(SchedulingRequest, SchedulingRequest.id == SchedulingSlotOffer.request_id)
                    .where(
                        SchedulingSlotOffer.candidate_id == candidate_id,
                        SchedulingSlotOffer.status == "open",
                        SchedulingRequest.status == "slots_offered",
                    )
                    .order_by(SchedulingSlotOffer.sent_at.desc())
                )
            ).scalars()
        )
        out = []
        for offer in rows:
            req = await request_row(db, offer.request_id)
            out.append(await _view(db, offer, req, None, portal=True))
        return out


__all__ = [
    "Subject",
    "counter",
    "link_subject",
    "offer_message",
    "pick",
    "pick_url",
    "portal_list",
    "portal_subject",
    "send",
    "set_timezone",
    "view",
]
