# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Scheduling Agent tools of the ``scheduling`` domain (Epic 59 Part B; ``owner_epic="59"``, ``version=1``).

| Key | Risk | Default after burn-in |
|---|---|---|
| ``calendar.free_busy`` / ``holidays.for_location`` / ``candidates.timezone`` / ``scheduling.check_times`` | read | auto |
| ``scheduling.propose`` | low | auto (internal: a draft offer) |
| ``scheduling.offer_slots`` | low (egress, templated) | auto |
| ``scheduling.offer_partial`` | high (egress) | Proposal only |
| ``reminders.send`` | low (egress, templated) | auto |

Free/busy only: no tool returns calendar event titles or content, and none creates a calendar hold.
"""

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import (
    IdempotencyRule,
    ProposalPreview,
    Recipient,
    ScopeRule,
    SendIntent,
    ToolOutcome,
    ToolSpec,
    register_tool,
)
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.services.agent_tools.scheduling.common_e59 import (
    OWNER_EPIC,
    VERSION,
    In,
    Out,
    load_request,
    refs_of,
    subject_application,
    svc,
)

REQ_SCOPE = ScopeRule(resource="scheduling_request", target_arg="request_id", cross_subject=True)
Channel = Literal["email", "whatsapp", "sms", "portal"]


def _preview(title: str, summary: Any) -> Any:
    async def preview(tctx: Any, args: Any) -> ProposalPreview:
        if getattr(args, "request_id", None) is not None:
            await load_request(tctx, args.request_id)
        if getattr(args, "offer_id", None) is not None:
            await _offer_and_request(tctx, args.offer_id, args.candidate_id)
        fields = tuple(
            (k, str(v))
            for k, v in args.model_dump(mode="json").items()
            if v is not None and k != "note_to_recruiter"
        )
        return ProposalPreview(title=title, summary=str(summary(args))[:4000], fields=fields[:12])

    return preview


# ================================================================ calendar.free_busy (read)
class FreeBusyIn(In):
    request_id: uuid.UUID
    start: datetime
    end: datetime


class BusyBlock(Out):
    start: datetime
    end: datetime


class UserBusy(Out):
    user_id: uuid.UUID
    status: Literal["available", "unknown"]
    busy: list[BusyBlock]


class FreeBusyOut(Out):
    users: list[UserBusy]


async def _free_busy(tctx: Any, args: FreeBusyIn) -> ToolOutcome:
    if args.end <= args.start or args.end - args.start > timedelta(days=14):
        raise ValidationFailed("The range must be positive and at most 14 days", code="invalid_range")
    req = await load_request(tctx, args.request_id)
    fb = await svc.availability.free_busy(
        tctx.request, tctx.db, list(req.participant_user_ids), args.start, args.end
    )
    users = [
        UserBusy(user_id=u, status=s, busy=[BusyBlock(start=a, end=b) for a, b in sorted(blocks)])  # type: ignore[arg-type]
        for u, (s, blocks) in fb.items()
    ]
    return ToolOutcome(
        FreeBusyOut(users=users),
        refs=refs_of(req),
        candidate_ids=(req.candidate_id,),
        summary=f"free/busy of {len(users)} interviewer(s)",
    )


register_tool(
    ToolSpec(
        key="calendar.free_busy", version=VERSION, owner_epic=OWNER_EPIC, title="Checked interviewers' free/busy",
        description=(
            "Busy blocks (start/end only) of the request's interviewers between start and end (at most 14 days). Only free/busy "
            "is ever read: never event titles or content. Interviewers without a connected calendar are 'unknown'."
        ),
        input_model=FreeBusyIn, output_model=FreeBusyOut, permissions=("calendar:free_busy",), risk="read",
        scope=REQ_SCOPE, handler=_free_busy,
    )
)  # fmt: skip


# ================================================================ holidays.for_location (read)
class HolidaysIn(In):
    request_id: uuid.UUID
    start: date
    end: date


class HolidayDay(Out):
    date: date
    name: str
    applies_to: str


class HolidaysOut(Out):
    days: list[HolidayDay]


async def _holidays(tctx: Any, args: HolidaysIn) -> ToolOutcome:
    if args.end < args.start or (args.end - args.start).days > 62:
        raise ValidationFailed("The range must be positive and at most 62 days", code="invalid_range")
    req = await load_request(tctx, args.request_id)
    av = svc.availability
    prefs = (await av.prefs_map(tctx.db, [req.owner_user_id])).get(req.owner_user_id)
    country = (
        (prefs.holiday_country if prefs is not None and prefs.holiday_country else None)
        or av.country_for_zone(await av.tenant_timezone(tctx.db))
        or "IN"
    )
    days = await av.holiday_days(
        tctx.db, country, prefs.holiday_region if prefs is not None else None, args.start, args.end
    )
    return ToolOutcome(
        HolidaysOut(days=[HolidayDay(**d) for d in days]),
        refs=refs_of(req),
        summary=f"{len(days)} holiday(s)",
    )


register_tool(
    ToolSpec(
        key="holidays.for_location", version=VERSION, owner_epic=OWNER_EPIC, title="Checked holidays",
        description="National and state holidays (from the tenant's holiday calendars) of the organiser's location between start and end (at most 62 days).",
        input_model=HolidaysIn, output_model=HolidaysOut, permissions=("interview:read",), risk="read", scope=REQ_SCOPE,
        handler=_holidays,
    )
)  # fmt: skip


# ================================================================ candidates.timezone (read)
class TimezoneIn(In):
    application_id: uuid.UUID


class TimezoneOut(Out):
    timezone: str
    source: Literal["request", "location", "tenant"]


async def _timezone(tctx: Any, args: TimezoneIn) -> ToolOutcome:
    if subject_application(tctx) != args.application_id:
        raise NotFound("Application not found")
    from app.models.pipeline import Application

    app = await tctx.db.get(Application, args.application_id)
    if app is None:
        raise NotFound("Application not found")
    tz, source = await svc.availability.candidate_timezone(tctx.db, app.candidate_id)
    return ToolOutcome(
        TimezoneOut(timezone=tz, source=source),
        candidate_ids=(app.candidate_id,),
        summary="candidate time zone",
    )  # type: ignore[arg-type]


register_tool(
    ToolSpec(
        key="candidates.timezone", version=VERSION, owner_epic=OWNER_EPIC, title="Looked up the candidate's time zone",
        description="The candidate's time zone from their location (country / state), else the tenant's zone. Never returns the address.",
        input_model=TimezoneIn, output_model=TimezoneOut, permissions=("candidate:read",), risk="read",
        scope=ScopeRule(resource="application", target_arg="application_id"), handler=_timezone,
    )
)  # fmt: skip


# ================================================================ scheduling.propose (low, internal)
class ProposeIn(In):
    request_id: uuid.UUID
    max_slots: int | None = Field(default=None, ge=1, le=10)


class SlotView(Out):
    index: int
    start: datetime
    end: datetime
    all_known_free: bool


class ProposeOut(Out):
    proposal_ref: uuid.UUID | None
    offer_id: uuid.UUID | None
    slots: list[SlotView]
    unknown_participants: list[uuid.UUID]
    partial: bool
    round: int
    request_status: str
    handed_off: bool = False


async def _propose(tctx: Any, args: ProposeIn) -> ToolOutcome:
    from app.services.scheduling_agent import service

    req = await load_request(tctx, args.request_id, for_update=True)
    if req.status not in ("open", "slots_offered", "picked", "counter_proposed"):
        raise ConflictError("The scheduling request is no longer open", code="scheduling_request_closed")
    try:
        offer = await service.compute_offer(tctx.request, tctx.db, req, max_slots=args.max_slots)
    except ConflictError as exc:
        if exc.code != "max_rounds_reached":
            raise
        await svc.requests.hand_off(tctx.request, tctx.db, req, "max_rounds")
        out = ProposeOut(
            proposal_ref=None,
            offer_id=None,
            slots=[],
            unknown_participants=[],
            partial=True,
            round=req.round,
            request_status=req.status,
            handed_off=True,
        )
        return ToolOutcome(
            out,
            refs=refs_of(req),
            candidate_ids=(req.candidate_id,),
            target=("scheduling_request", req.id),
            summary="max rounds reached: handed to the recruiter",
        )
    slots = [
        SlotView(index=i, start=s.start, end=s.end, all_known_free=s.all_known_free)
        for i, s in enumerate(service.offer_slots(offer))
    ]
    out = ProposeOut(
        proposal_ref=offer.id, offer_id=offer.id, slots=slots, unknown_participants=list(offer.unknown_participants or []),
        partial=offer.partial, round=offer.round, request_status=req.status,
    )  # fmt: skip
    return ToolOutcome(
        out, refs=refs_of(req, ("scheduling_slot_offer", offer.id)), candidate_ids=(req.candidate_id,),
        target=("scheduling_request", req.id), summary=f"{len(slots)} slot(s){' (partial)' if offer.partial else ''}",
    )  # fmt: skip


register_tool(
    ToolSpec(
        key="scheduling.propose", version=VERSION, owner_epic=OWNER_EPIC, title="Computed interview slots",
        description=(
            "Compute up to max_slots interview slots for the request in the next business days, only where every connected "
            "interviewer is free (free/busy only), inside everyone's working hours, off holidays and inside the candidate's "
            "hours. Stores them as a draft offer (offer_id). partial=true means no common free slot: use scheduling.offer_partial. "
            "At the maximum number of rounds the request is handed to the recruiter (handed_off=true). Creates no calendar hold."
        ),
        input_model=ProposeIn, output_model=ProposeOut, permissions=("calendar:free_busy", "interview:schedule"), risk="low",
        scope=REQ_SCOPE, handler=_propose, preview=_preview("Compute interview slots", lambda a: f"Compute slots for request {a.request_id}"),
        default_mode="auto", max_calls_per_run=4,
    )
)  # fmt: skip


# ================================================================ scheduling.check_times (read)
class WindowIn(In):
    start: datetime
    end: datetime


class CheckTimesIn(In):
    request_id: uuid.UUID
    windows: list[WindowIn] = Field(default_factory=list, max_length=3)


class WindowSlot(Out):
    start: datetime
    end: datetime


class CheckTimesOut(Out):
    matches: list[WindowSlot]
    conflicts: int
    windows_checked: int


async def _check_times(tctx: Any, args: CheckTimesIn) -> ToolOutcome:
    from app.services.scheduling_agent import service

    req = await load_request(tctx, args.request_id)
    windows = [(w.start, w.end) for w in args.windows]
    if not windows:
        latest = await svc.requests.latest_offer(tctx.db, req.id, "declined")
        windows = [
            (datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
            for w in ((latest.counter_windows or []) if latest is not None else [])
        ]
    matches, busy = await service.check_times(tctx.request, tctx.db, req, windows)
    out = CheckTimesOut(
        matches=[WindowSlot(start=m.start, end=m.end) for m in matches[:10]],
        conflicts=len(busy),
        windows_checked=len(windows),
    )
    return ToolOutcome(
        out, refs=refs_of(req), candidate_ids=(req.candidate_id,), summary=f"{len(matches)} matching slot(s)"
    )


register_tool(
    ToolSpec(
        key="scheduling.check_times", version=VERSION, owner_epic=OWNER_EPIC, title="Checked the candidate's suggested times",
        description=(
            "Check up to 3 candidate-suggested windows (default: the windows of the candidate's latest counter-proposal) against "
            "the interviewers' free/busy, working hours and holidays. Returns the free slots inside them (matches)."
        ),
        input_model=CheckTimesIn, output_model=CheckTimesOut, permissions=("calendar:free_busy",), risk="read",
        scope=REQ_SCOPE, handler=_check_times,
    )
)  # fmt: skip


# ================================================================ scheduling.offer_slots / offer_partial (egress)
class OfferIn(In):
    offer_id: uuid.UUID
    candidate_id: uuid.UUID
    channel: Channel = "email"


class OfferPartialIn(OfferIn):
    note_to_recruiter: str = Field(default="", max_length=500)


class OfferSentOut(Out):
    message_id: uuid.UUID | None
    link_expires_at: datetime | None


async def _offer_and_request(tctx: Any, offer_id: uuid.UUID, candidate_id: uuid.UUID) -> tuple[Any, Any]:
    from app.models.scheduling_agent import SchedulingSlotOffer

    offer = await tctx.db.get(SchedulingSlotOffer, offer_id)
    if offer is None or offer.candidate_id != candidate_id:
        raise NotFound("Offer not found")
    req = await load_request(tctx, offer.request_id)
    return offer, req


async def _send(tctx: Any, args: OfferIn, *, partial_ok: bool) -> ToolOutcome:
    offer, req = await _offer_and_request(tctx, args.offer_id, args.candidate_id)
    data = await svc.offers.send(tctx.request, tctx.db, offer.id, args.channel, partial_ok=partial_ok)
    out = OfferSentOut(message_id=data["message_id"], link_expires_at=data["link_expires_at"])
    return ToolOutcome(
        out, refs=refs_of(req, ("scheduling_slot_offer", offer.id)), candidate_ids=(req.candidate_id,),
        target=("candidate", req.candidate_id), summary=f"offer sent ({args.channel})",
    )  # fmt: skip


async def _offer_slots(tctx: Any, args: OfferIn) -> ToolOutcome:
    return await _send(tctx, args, partial_ok=False)


async def _offer_partial(tctx: Any, args: OfferPartialIn) -> ToolOutcome:
    return await _send(tctx, args, partial_ok=True)


async def _offer_intent(tctx: Any, args: OfferIn) -> SendIntent:
    _, req = await _offer_and_request(tctx, args.offer_id, args.candidate_id)
    channel = "email" if args.channel == "sms" else args.channel
    return SendIntent(
        recipients=(Recipient(kind="candidate", id=req.candidate_id, channel=channel),),  # type: ignore[arg-type]
        templated=True, template_ref="scheduling_offer", text_for_checks="",
    )  # fmt: skip


def _offer_version(probe: Any) -> Any:
    async def version() -> str:
        from app.models.scheduling_agent import SchedulingSlotOffer

        offer = await probe.db.get(SchedulingSlotOffer, probe.args.offer_id)
        return f"{offer.status}:{offer.round}" if offer is not None else "missing"

    return version()


register_tool(
    ToolSpec(
        key="scheduling.offer_slots", version=VERSION, owner_epic=OWNER_EPIC, title="Offered interview times to the candidate",
        description=(
            "Send a draft offer (from scheduling.propose) to the request's candidate as the approved 'pick a time' message with "
            "a self-scheduling link (email; whatsapp uses the tenant's approved template; portal adds a portal card). Times are "
            "shown in the candidate's time zone. Refuses a partial offer (use scheduling.offer_partial)."
        ),
        input_model=OfferIn, output_model=OfferSentOut, permissions=("message:send_templated", "interview:schedule"),
        risk="low", scope=ScopeRule(resource="candidate", target_arg="candidate_id", candidate_arg="candidate_id", cross_subject=True, version=_offer_version),
        handler=_offer_slots, egress=True, send_intent=_offer_intent,
        preview=_preview("Offer interview times", lambda a: f"Send the self-scheduling link for offer {a.offer_id} by {a.channel}"),
        idempotency=IdempotencyRule(fields=("offer_id",), per_run=False), default_mode="auto", max_calls_per_run=4,
    )
)  # fmt: skip

register_tool(
    ToolSpec(
        key="scheduling.offer_partial", version=VERSION, owner_epic=OWNER_EPIC, title="Offer partial interview options",
        description=(
            "When no slot has every connected interviewer free (partial=true), propose to the recruiter to send the best partial "
            "options to the candidate. Always a Proposal: the recruiter confirms first. note_to_recruiter explains the gaps."
        ),
        input_model=OfferPartialIn, output_model=OfferSentOut, permissions=("message:send_templated", "interview:schedule"),
        risk="high", scope=ScopeRule(resource="candidate", target_arg="candidate_id", candidate_arg="candidate_id", cross_subject=True, version=_offer_version),
        handler=_offer_partial, egress=True, send_intent=_offer_intent,
        preview=_preview("Offer partial interview options", lambda a: f"No slot has every interviewer free. Send the best partial options (offer {a.offer_id}) by {a.channel}. {a.note_to_recruiter}"),
        idempotency=IdempotencyRule(fields=("offer_id",), per_run=False), default_mode="suggest", max_calls_per_run=2,
    )
)  # fmt: skip


# ================================================================ reminders.send (egress, templated)
class ReminderIn(In):
    reminder_id: uuid.UUID


class ReminderOut(Out):
    status: str
    channel: str | None


async def _reminder_row(tctx: Any, reminder_id: uuid.UUID) -> Any:
    """The reminder must be in this run's batch (dispatched to it), or -- undispatched -- belong to an interview of the
    run's Application; anything else is ``not_found``."""
    from app.models.interviews import Interview
    from app.models.scheduling_agent import SchedulingReminder

    row = await tctx.db.get(SchedulingReminder, reminder_id)
    if row is None:
        raise NotFound("Reminder not found")
    if row.run_id is not None and row.run_id == tctx.run_id:
        return row
    app_id = subject_application(tctx)
    interview = (
        await tctx.db.get(Interview, row.interview_id) if row.run_id is None and app_id is not None else None
    )
    if interview is None or interview.application_id != app_id:
        raise NotFound("Reminder not found")
    return row


async def _reminder(tctx: Any, args: ReminderIn) -> ToolOutcome:
    row = await _reminder_row(tctx, args.reminder_id)
    data = await svc.reminders.send(tctx.request, tctx.db, args.reminder_id, row.run_id)
    return ToolOutcome(
        ReminderOut(**data),
        target=("scheduling_reminder", args.reminder_id),
        summary=f"reminder {data['status']}",
    )


async def _reminder_intent(tctx: Any, args: ReminderIn) -> SendIntent:
    row = await _reminder_row(tctx, args.reminder_id)
    recipients = (
        (Recipient(kind="candidate", id=row.candidate_id, channel="email"),) if row.candidate_id else ()
    )
    return SendIntent(
        recipients=recipients, templated=True, template_ref="scheduling_reminder", text_for_checks=""
    )


register_tool(
    ToolSpec(
        key="reminders.send", version=VERSION, owner_epic=OWNER_EPIC, title="Sent an interview reminder",
        description=(
            "Send one due reminder or scorecard nudge from this run's batch (reminder_ids in the task): the approved reminder "
            "text in the recipient's time zone -- candidate by email / WhatsApp within consent, internal interviewers in-app and "
            "by email, external interviewers by email. A reminder already sent or no longer due is not sent again."
        ),
        input_model=ReminderIn, output_model=ReminderOut, permissions=("message:send_templated",), risk="low",
        scope=ScopeRule(resource="scheduling_reminder", target_arg="reminder_id", cross_subject=True), handler=_reminder,
        egress=True, send_intent=_reminder_intent, preview=_preview("Send an interview reminder", lambda a: f"Send reminder {a.reminder_id}"),
        idempotency=IdempotencyRule(fields=("reminder_id",), per_run=False), default_mode="auto", max_calls_per_run=10,
    )
)  # fmt: skip
