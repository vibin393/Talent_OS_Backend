# ruff: file-ignore[line-too-long]
"""Recruitment events: setup, stations, registration, check-in (Epic 43 story 43.5, FR-220).

* Events (``recruitment_events``): ``draft -> open`` (issues the public registration link / QR, purpose
  ``event_checkin``, subject ``recruitment_event``) ``-> live -> closed``; ``draft | open | live -> cancelled``
  (revokes links). Every other transition 409.
* Public registration (``/public/campus/e/{token}``): rate limits, bot challenge, the ``campus`` notice, optional
  ``marketing`` and -- when the event notifies by WhatsApp -- the P2 ``whatsapp`` opt-in (source ``event_checkin``).
  Each registrant gets a personal single-use check-in link (subject ``event_checkin``) by email and as a QR.
* Check-in (kiosk, ``event:manage``): the personal QR token, an existing Candidate (search) or a walk-in form ->
  ``checked_in``, an Application on the chosen Demand (``source="event"``, ``source_ref=str(checkin_id)``; an
  existing Application is reused) and a queue entry with the next ``position`` (per-event sequence under the
  event row lock). Walk-ins are created through P1 duplicate detection; no consent is inferred for them.
"""

import hashlib
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, bot_challenge, rate_limit, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.campus import EventCheckin, EventStation, QueueEntry, RecruitmentEvent
from app.schemas.campus import (
    CheckInIn,
    CheckinOut,
    DemandRef,
    EventCheckinsOut,
    EventCounts,
    EventIn,
    EventOut,
    EventPatch,
    StationIn,
    StationOut,
    StationPatch,
)
from app.schemas.campus_public import (
    PublicEventDemand,
    PublicEventRegisterIn,
    PublicEventRegisterOut,
    PublicEventView,
)
from app.services.campus import links
from app.services.campus.common import (
    CONSENT_SOURCE_EVENT,
    EVENT_MANAGE,
    LINK_EVENT,
    SUBJECT_CHECKIN,
    SUBJECT_EVENT,
    candidate_facts,
    conflict,
    demand_refs,
    event_for,
    event_url,
    invalid,
    now,
    principal_of,
    settings_row,
    tenant_of,
    user_id_of,
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"open", "cancelled"}),
    "open": frozenset({"live", "closed", "cancelled"}),
    "live": frozenset({"closed", "cancelled"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}
RUNNING = ("open", "live")


# ================================================================ read models
async def event_out(db: AsyncSession, event: RecruitmentEvent) -> EventOut:
    refs = await demand_refs(db, list(event.demand_ids or []))
    link_rows = await links.link_state(db, [event.registration_link_id] if event.registration_link_id else [])
    active = links.active(link_rows.get(event.registration_link_id)) if event.registration_link_id else False
    token = links.token_of(event.tenant_id, event.token_sealed) if active else None
    checkins = dict((await db.execute(select(EventCheckin.status, func.count()).where(EventCheckin.event_id == event.id).group_by(EventCheckin.status))).tuples().all())
    queue = dict((await db.execute(select(QueueEntry.status, func.count()).where(QueueEntry.event_id == event.id).group_by(QueueEntry.status))).tuples().all())
    stations = int((await db.execute(select(func.count()).select_from(EventStation).where(EventStation.event_id == event.id))).scalar_one())
    return EventOut(
        id=event.id, name=event.name, kind=event.kind, drive_id=event.drive_id, owner_id=event.owner_id,  # type: ignore[arg-type]
        demands=[DemandRef(**refs[d]) for d in event.demand_ids or [] if d in refs], starts_at=event.starts_at,
        ends_at=event.ends_at, timezone=event.timezone, venue=event.venue, video_link=event.video_link,
        status=event.status, registration_url=event_url(token) if token else None,  # type: ignore[arg-type]
        avg_interview_minutes=event.avg_interview_minutes, notify_channel=event.notify_channel,  # type: ignore[arg-type]
        station_count=stations,
        counts=EventCounts(
            registered=sum(int(v) for v in checkins.values()), checked_in=int(checkins.get("checked_in", 0)),
            waiting=int(queue.get("waiting", 0)) + int(queue.get("called", 0)), in_interview=int(queue.get("in_interview", 0)),
            interviewed=int(queue.get("done", 0)),
        ),
        cancel_reason=event.cancel_reason, created_at=event.created_at,
    )


async def list_events(c: RequestContext, db: AsyncSession, *, status: str | None = None) -> list[EventOut]:
    from app.core.permissions import scope_condition

    stmt = select(RecruitmentEvent)
    p = principal_of(c)
    if p is not None:
        cond = scope_condition(p, EVENT_MANAGE if p.has(EVENT_MANAGE) else "campus:read", RecruitmentEvent.owner_id)
        if cond is not None:
            stmt = stmt.where(cond)
    if status:
        stmt = stmt.where(RecruitmentEvent.status == status)
    rows = list((await db.execute(stmt.order_by(RecruitmentEvent.starts_at.desc()).limit(500))).scalars())
    return [await event_out(db, e) for e in rows]


async def get_event(c: RequestContext, db: AsyncSession, event_id: uuid.UUID) -> EventOut:
    return await event_out(db, await event_for(c, db, event_id))


async def _check_demands(db: AsyncSession, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    from app.models.demands import Demand

    wanted = list(dict.fromkeys(ids))
    found = set((await db.execute(select(Demand.id).where(Demand.id.in_(wanted), Demand.archived_at.is_(None)))).scalars()) if wanted else set()
    if set(wanted) - found:
        raise invalid("unknown_demand", "Unknown demand", {"demand_ids": "Unknown demand"})
    return wanted


async def create_event(c: RequestContext, db: AsyncSession, data: EventIn) -> EventOut:
    from app.services.campus.common import drive_for

    if data.drive_id is not None:
        await drive_for(c, db, data.drive_id)
    owner = user_id_of(c)
    if owner is None:
        raise invalid("owner_required", "An event needs an owner")
    settings = await settings_row(db)
    if data.video_link and not data.video_link.lower().startswith(("https://", "http://")):
        raise invalid("invalid_url", "The video link must be an http(s) URL", {"video_link": "Invalid URL"})
    event = RecruitmentEvent(
        id=uuid.uuid4(), tenant_id=tenant_of(db), name=data.name.strip(), kind=data.kind, drive_id=data.drive_id,
        owner_id=owner, demand_ids=await _check_demands(db, data.demand_ids), starts_at=data.starts_at, ends_at=data.ends_at,
        timezone=data.timezone, venue=data.venue, video_link=data.video_link,
        avg_interview_minutes=data.avg_interview_minutes or settings.avg_interview_minutes,
        notify_channel=data.notify_channel, status="draft",
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return await event_out(db, event)


async def update_event(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, data: EventPatch) -> EventOut:
    event = await event_for(c, db, event_id, lock=True)
    if event.status in ("closed", "cancelled"):
        raise conflict("event_closed", "This event is closed", status=event.status)
    changes = data.model_dump(exclude_unset=True)
    if "demand_ids" in changes and changes["demand_ids"] is not None:
        event.demand_ids = await _check_demands(db, changes.pop("demand_ids"))
    for key, value in changes.items():
        if value is None and key in ("name", "starts_at", "ends_at", "timezone", "avg_interview_minutes", "notify_channel"):
            continue
        setattr(event, key, value)
    if event.ends_at <= event.starts_at:
        raise invalid("invalid_window", "The event must end after it starts", {"ends_at": "Must be after the start"})
    await db.flush()
    await db.refresh(event)
    return await event_out(db, event)


async def transition(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, to: str, *, reason: str | None = None) -> EventOut:
    event = await event_for(c, db, event_id, lock=True)
    if to not in TRANSITIONS.get(event.status, frozenset()):
        raise conflict("transition_not_allowed", f"A {event.status} event cannot move to {to}", status=event.status, to=to)
    if to == "open":
        if event.ends_at <= now():
            raise conflict("event_ended", "The event has already ended")
        await links.issue_event_link(c, db, event)
    if to in ("closed", "cancelled"):
        if event.registration_link_id is not None:
            await secure_links.revoke(db, event.registration_link_id)
        await secure_links_for_checkins(db, event.id)
        if to == "cancelled":
            event.cancel_reason = (reason or "").strip()[:500] or None
    event.status = to
    await db.flush()
    audit.record(f"campus.event_{to}", "recruitment_event", event.id, session=db)
    await db.refresh(event)
    return await event_out(db, event)


async def secure_links_for_checkins(db: AsyncSession, event_id: uuid.UUID) -> int:
    ids = list((await db.execute(select(EventCheckin.checkin_link_id).where(EventCheckin.event_id == event_id, EventCheckin.checkin_link_id.is_not(None)))).scalars())
    count = 0
    for link_id in ids:
        if await secure_links.revoke(db, link_id):
            count += 1
    return count


# ================================================================ stations
def _station_out(s: EventStation, current: uuid.UUID | None = None) -> StationOut:
    return StationOut(
        id=s.id, event_id=s.event_id, label=s.label, interviewer_user_ids=list(s.interviewer_user_ids or []),
        demand_id=s.demand_id, status=s.status, current_entry_id=current,  # type: ignore[arg-type]
    )


async def station_outs(db: AsyncSession, event_id: uuid.UUID) -> list[StationOut]:
    rows = list((await db.execute(select(EventStation).where(EventStation.event_id == event_id).order_by(EventStation.created_at, EventStation.label))).scalars())
    current = dict(
        (
            await db.execute(
                select(QueueEntry.station_id, QueueEntry.id).where(
                    QueueEntry.event_id == event_id, QueueEntry.status.in_(("called", "in_interview")), QueueEntry.station_id.is_not(None)
                )
            )
        ).tuples().all()
    )
    return [_station_out(s, current.get(s.id)) for s in rows]


async def list_stations(c: RequestContext, db: AsyncSession, event_id: uuid.UUID) -> list[StationOut]:
    event = await event_for(c, db, event_id)
    return await station_outs(db, event.id)


async def _interviewers(db: AsyncSession, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    from app.models.identity import User

    wanted = list(dict.fromkeys(ids))
    found = set((await db.execute(select(User.id).where(User.id.in_(wanted), User.is_active.is_(True)))).scalars()) if wanted else set()
    if set(wanted) - found:
        raise invalid("unknown_interviewer", "Every interviewer must be an active user", {"interviewer_user_ids": "Unknown user"})
    return wanted


async def add_station(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, data: StationIn) -> StationOut:
    event = await event_for(c, db, event_id, lock=True)
    if event.status in ("closed", "cancelled"):
        raise conflict("event_closed", "This event is closed")
    if data.demand_id is not None and data.demand_id not in (event.demand_ids or []):
        raise conflict("demand_not_on_event", "Choose one of the event's demands")
    s = EventStation(
        id=uuid.uuid4(), tenant_id=event.tenant_id, event_id=event.id, label=data.label.strip(),
        interviewer_user_ids=await _interviewers(db, data.interviewer_user_ids), demand_id=data.demand_id, status="open",
    )
    db.add(s)
    await db.flush()
    return _station_out(s)


async def update_station(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, station_id: uuid.UUID, data: StationPatch) -> StationOut:
    event = await event_for(c, db, event_id)
    s = await db.get(EventStation, station_id, with_for_update=True)
    if s is None or s.event_id != event.id:
        raise NotFound("Station not found")
    changes = data.model_dump(exclude_unset=True)
    if changes.get("label"):
        s.label = changes["label"].strip()
    if changes.get("interviewer_user_ids") is not None:
        s.interviewer_user_ids = await _interviewers(db, changes["interviewer_user_ids"])
    if "demand_id" in changes:
        if changes["demand_id"] is not None and changes["demand_id"] not in (event.demand_ids or []):
            raise conflict("demand_not_on_event", "Choose one of the event's demands")
        s.demand_id = changes["demand_id"]
    if changes.get("status"):
        s.status = changes["status"]
    await db.flush()
    return _station_out(s)


# ================================================================ check-ins
async def checkin_outs(db: AsyncSession, rows: list[EventCheckin]) -> list[CheckinOut]:
    facts = await candidate_facts(db, [r.candidate_id for r in rows])
    refs = await demand_refs(db, [r.demand_id for r in rows if r.demand_id])
    entries = {
        e.checkin_id: e
        for e in (await db.execute(select(QueueEntry).where(QueueEntry.checkin_id.in_([r.id for r in rows])))).scalars()
    } if rows else {}
    out: list[CheckinOut] = []
    for r in rows:
        e = entries.get(r.id)
        out.append(
            CheckinOut(
                id=r.id, event_id=r.event_id, candidate_id=r.candidate_id, candidate_name=facts.get(r.candidate_id, (None, None))[0],
                demand_id=r.demand_id, demand_title=refs.get(r.demand_id, {}).get("title") if r.demand_id else None,
                status=r.status, method=r.method, registered_at=r.registered_at, checked_in_at=r.checked_in_at,  # type: ignore[arg-type]
                queue_entry_id=e.id if e else None, queue_position=e.position if e else None,
                queue_status=e.status if e else None,  # type: ignore[arg-type]
            )
        )
    return out


async def list_checkins(
    c: RequestContext, db: AsyncSession, event_id: uuid.UUID, *, status: str | None, q: str | None, limit: int, offset: int
) -> EventCheckinsOut:
    from app.models.candidates import Candidate

    event = await event_for(c, db, event_id)
    stmt = select(EventCheckin).where(EventCheckin.event_id == event.id)
    if status:
        stmt = stmt.where(EventCheckin.status == status)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(EventCheckin.candidate_id.in_(select(Candidate.id).where(Candidate.full_name.ilike(like, escape="\\"))))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = list((await db.execute(stmt.order_by(EventCheckin.registered_at, EventCheckin.id).limit(limit).offset(offset))).scalars())
    return EventCheckinsOut(items=await checkin_outs(db, rows), total=total)


def _token_from(raw: str) -> str:
    """Accept the bare token or the scanned URL (``.../campus/e/<token>`` or ``.../l/<token>``)."""
    text = raw.strip().rstrip("/")
    return text.rsplit("/", 1)[-1] if "/" in text else text


async def next_position(db: AsyncSession, event_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                update(RecruitmentEvent)
                .where(RecruitmentEvent.id == event_id)
                .values(next_position=RecruitmentEvent.next_position + 1)
                .returning(RecruitmentEvent.next_position)
            )
        ).scalar_one()
    )


async def check_in(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, data: CheckInIn) -> CheckinOut:
    from app.models.candidates import Candidate
    from app.services import pipeline_service
    from app.services.campus.registration import find_or_create_candidate

    event = await event_for(c, db, event_id, lock=True)
    if event.status not in RUNNING:
        raise conflict("event_not_running", "Check-in opens when the event is open or live", status=event.status)
    checkin: EventCheckin | None = None
    method = "manual"
    if data.token is not None:
        link = await secure_links.resolve(_token_from(data.token), (LINK_EVENT,))
        if link.tenant_id != event.tenant_id or link.subject_type != SUBJECT_CHECKIN:
            raise NotFound("Check-in code not found", code="link_invalid")
        checkin = await db.get(EventCheckin, link.subject_id, with_for_update=True)
        if checkin is None:
            raise NotFound("Check-in code not found", code="link_invalid")
        if checkin.event_id != event.id:
            raise conflict("wrong_event", "This code belongs to another event")
        await secure_links.consume(db, link.id)
        method = "qr"
        candidate_id = checkin.candidate_id
    elif data.candidate_id is not None:
        cand = await db.get(Candidate, data.candidate_id)
        if cand is None or cand.erased_at is not None or cand.merged_into_id is not None:
            raise NotFound("Candidate not found")
        candidate_id = cand.id
    else:
        assert data.walk_in is not None
        candidate_id, _ = await find_or_create_candidate(
            c, db, full_name=data.walk_in.full_name, email=str(data.walk_in.email), phone=data.walk_in.phone,
            owner_id=event.owner_id, source="event", source_detail=f"Recruitment event: {event.name}",
        )
        method = "walk_in"
    if checkin is None:
        checkin = (
            await db.execute(select(EventCheckin).where(EventCheckin.event_id == event.id, EventCheckin.candidate_id == candidate_id).with_for_update())
        ).scalar_one_or_none()
    demand_id = data.demand_id or (checkin.demand_id if checkin else None)
    if demand_id is None and len(event.demand_ids or []) == 1:
        demand_id = event.demand_ids[0]
    if demand_id is not None and demand_id not in (event.demand_ids or []):
        raise conflict("demand_not_on_event", "Choose one of the event's demands")
    at = now()
    if checkin is None:
        checkin = EventCheckin(
            id=uuid.uuid4(), tenant_id=event.tenant_id, event_id=event.id, candidate_id=candidate_id, demand_id=demand_id,
            status="checked_in", method=method, registered_at=at, checked_in_at=at,
        )
        db.add(checkin)
        await db.flush()
    else:
        existing = (await db.execute(select(QueueEntry).where(QueueEntry.checkin_id == checkin.id))).scalar_one_or_none()
        if existing is not None and checkin.status == "checked_in":
            return (await checkin_outs(db, [checkin]))[0]
        checkin.status = "checked_in"
        checkin.method = method
        checkin.checked_in_at = at
        checkin.demand_id = demand_id
    application_id = None
    if demand_id is not None:
        application_id = await pipeline_service.add_candidate(c, db, demand_id, candidate_id, source="event", source_ref=str(checkin.id))
    db.add(
        QueueEntry(
            id=uuid.uuid4(), tenant_id=event.tenant_id, event_id=event.id, checkin_id=checkin.id, candidate_id=candidate_id,
            demand_id=demand_id, status="waiting", position=await next_position(db, event.id), application_id=application_id,
        )
    )
    await db.flush()
    audit.record("campus.checked_in", "recruitment_event", event.id, session=db, method=method)
    return (await checkin_outs(db, [checkin]))[0]


# ================================================================ public pages
def _ip_hash(ip: str | None) -> str:
    return hashlib.sha256((ip or "unknown").encode()).hexdigest()[:32]


async def _event_from_link(db: AsyncSession, link: secure_links.ResolvedLink) -> tuple[RecruitmentEvent, EventCheckin | None]:
    checkin: EventCheckin | None = None
    if link.subject_type == SUBJECT_EVENT:
        event = await db.get(RecruitmentEvent, link.subject_id)
    elif link.subject_type == SUBJECT_CHECKIN:
        checkin = await db.get(EventCheckin, link.subject_id)
        event = await db.get(RecruitmentEvent, checkin.event_id) if checkin else None
    else:
        event = None
    if event is None:
        raise NotFound("Link not found", code="link_invalid")
    return event, checkin


async def public_view(token: str, *, ip: str | None) -> PublicEventView:
    from app.services import identity_bridge
    from app.services.campus.registration import _notice

    await rate_limit.enforce("campus_view_ip", _ip_hash(ip), 120, 3600)
    link = await secure_links.resolve(token, (LINK_EVENT,))
    async with tenant_session(link.tenant_id) as db:
        event, checkin = await _event_from_link(db, link)
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        refs = await demand_refs(db, list(event.demand_ids or []))
        open_ = event.status in RUNNING and event.ends_at > now()
        base: dict[str, Any] = {
            "status": "open" if open_ else "closed",
            "tenant_name": tenant.name if tenant else None,
            "event_name": event.name,
            "kind": event.kind,
            "starts_at": event.starts_at,
            "ends_at": event.ends_at,
            "timezone": event.timezone,
            "venue": event.venue,
            "video_link": event.video_link,
            "demands": [PublicEventDemand(id=d, title=refs[d]["title"]) for d in event.demand_ids or [] if d in refs],
            "whatsapp_opt_in": event.notify_channel == "whatsapp",
            "challenge": bot_challenge.public_config(),
        }
        if checkin is not None:
            facts = await candidate_facts(db, [checkin.candidate_id])
            name = facts.get(checkin.candidate_id, (None, None))[0]
            url = event_url(token)
            return PublicEventView(
                mode="pass", notice=None, marketing_notice=None, candidate_first_name=(name or "").split(" ")[0] or None,
                checkin_url=url, qr_svg=links.qr_svg_inline(url), **base,
            )
        return PublicEventView(
            mode="register", notice=await _notice(db, link.tenant_id, "campus"),
            marketing_notice=await _notice(db, link.tenant_id, "marketing"), **base,
        )


async def _send_pass(db: AsyncSession, event: RecruitmentEvent, email: str, first_name: str, url: str) -> None:
    from app.core.mailer import EmailMessage, send_email
    from app.services import identity_bridge

    tenant = await identity_bridge.get_tenant(db, event.tenant_id)
    brand = tenant.name if tenant is not None else "our team"
    where = event.venue or event.video_link or ""
    await send_email(
        EmailMessage(
            to=email,
            subject=f"Your check-in pass for {event.name}"[:200],
            text=(
                f"Hello {first_name or 'there'},\n\nYou are registered for {event.name} on {event.starts_at:%d %b %Y}"
                f"{' at ' + where if where else ''}.\n\nShow this check-in pass (open the link to see your QR code) at the "
                f"registration desk:\n{url}\n\nThe pass works once.\n\n{brand}\n"
            ),
            tenant_id=event.tenant_id,
            kind="event_checkin_pass",
            from_name=brand,
        )
    )


async def public_register(token: str, payload: PublicEventRegisterIn, *, ip: str | None, user_agent: str | None) -> PublicEventRegisterOut:
    from datetime import timedelta

    from app.services.campus.common import CONSENT_PURPOSE
    from app.services.campus.registration import _check_notice, find_or_create_candidate, record_consents
    from app.services.candidates.common import normalize_email

    link = await secure_links.resolve(token, (LINK_EVENT,))
    if link.subject_type != SUBJECT_EVENT:
        raise NotFound("Link not found", code="link_invalid")
    async with tenant_session(link.tenant_id) as db:
        settings = await settings_row(db)
        ip_limit, drive_limit = settings.register_ip_limit, settings.register_drive_limit
    await rate_limit.enforce("campus_register_ip", _ip_hash(ip), ip_limit, 3600)
    await rate_limit.enforce("campus_register_event", link.subject_id.hex, drive_limit, 600)
    await bot_challenge.verify_or_fail(payload.bot_token, ip)
    email = normalize_email(str(payload.email))
    if email is None:
        raise invalid("invalid_email", "Enter a valid email address", {"email": "Enter a valid email address"})
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "system", None)
        event, _ = await _event_from_link(db, link)
        if event.status not in RUNNING or event.ends_at <= now():
            raise conflict("registration_closed", "Registration for this event is closed")
        if payload.demand_id is not None and payload.demand_id not in (event.demand_ids or []):
            raise invalid("unknown_demand", "Choose one of the listed roles", {"demand_id": "Unknown role"})
        notice = await _check_notice(db, link.tenant_id, payload.notice_id, payload.agreed)
        c = RequestContext(tenant_id=link.tenant_id, ip=ip, user_agent=user_agent)
        candidate_id, _ = await find_or_create_candidate(
            c, db, full_name=payload.full_name, email=email, phone=payload.phone, owner_id=event.owner_id,
            source="event", source_detail=f"Recruitment event: {event.name}",
        )
        dup = (await db.execute(select(EventCheckin.id).where(EventCheckin.event_id == event.id, EventCheckin.candidate_id == candidate_id))).first()
        if dup is not None:
            return PublicEventRegisterOut(status="already_registered")
        checkin = EventCheckin(
            id=uuid.uuid4(), tenant_id=event.tenant_id, event_id=event.id, candidate_id=candidate_id,
            demand_id=payload.demand_id, status="registered", method="registration", registered_at=now(),
        )
        db.add(checkin)
        await db.flush()
        audit.set_actor(db, "candidate", candidate_id)
        checkin.consent_id = await record_consents(
            db, candidate_id, notice=notice, marketing=payload.marketing_opt_in, source=CONSENT_SOURCE_EVENT, ip=ip,
            evidence={"event_id": str(event.id)},
        )
        if payload.whatsapp_opt_in and event.notify_channel == "whatsapp":
            from app.services import privacy

            await privacy.consent.record(db, candidate_id, "whatsapp", CONSENT_SOURCE_EVENT, None, None, evidence={"event_id": str(event.id), "ip_hash": _ip_hash(ip)})
        left = event.ends_at - now()
        personal, personal_token = await secure_links.issue(
            db, tenant_id=event.tenant_id, purpose=LINK_EVENT, subject_type=SUBJECT_CHECKIN, subject_id=checkin.id,
            ttl=left if left > timedelta(hours=1) else timedelta(hours=1), max_uses=1, metadata={"event_id": str(event.id)},
        )
        checkin.checkin_link_id = personal.id
        await db.flush()
        url = event_url(personal_token)
        await _send_pass(db, event, email, payload.full_name.split(" ")[0], url)
        audit.record("campus.event_registered", "recruitment_event", event.id, session=db, purpose=CONSENT_PURPOSE)
    return PublicEventRegisterOut(status="registered", checkin_url=url, qr_svg=links.qr_svg_inline(url))
