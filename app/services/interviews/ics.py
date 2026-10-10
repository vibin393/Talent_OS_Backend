"""iCalendar (RFC 5545) invitations for interviews (AD-9, FR-40), built with ``icalendar``.

``UID`` = ``interviews.ical_uid`` (stable across reschedules), ``SEQUENCE`` = ``interviews.sequence``,
``DTSTART``/``DTEND`` in UTC (``Z``), ``ORGANIZER``, one ``ATTENDEE`` per participant with ``PARTSTAT``,
``LOCATION`` / ``URL``, a ``DESCRIPTION`` naming the recipient's time zone, ``METHOD:REQUEST|CANCEL`` and
``STATUS:CONFIRMED|CANCELLED``. Attached as ``invite.ics`` (``text/calendar; method=...; charset=UTF-8``).
"""

import dataclasses
from datetime import UTC, datetime

from icalendar import Calendar, Event, vCalAddress, vText

from app.core.mailer import EmailAttachment

PRODID = "-//Talent OS//Interviews//EN"
PARTSTAT = {"needs_action": "NEEDS-ACTION", "accepted": "ACCEPTED", "declined": "DECLINED"}


@dataclasses.dataclass(frozen=True, slots=True)
class IcsAttendee:
    email: str
    name: str | None
    response_status: str = "needs_action"
    role: str = "REQ-PARTICIPANT"


@dataclasses.dataclass(frozen=True, slots=True)
class IcsEvent:
    uid: str
    sequence: int
    summary: str
    starts_at: datetime
    ends_at: datetime
    organizer_email: str
    organizer_name: str | None
    attendees: tuple[IcsAttendee, ...]
    recipient_timezone: str
    description_lines: tuple[str, ...] = ()
    location: str | None = None
    url: str | None = None
    cancelled: bool = False
    stamp: datetime | None = None


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _address(email: str, name: str | None, **params: str) -> vCalAddress:
    addr = vCalAddress(f"mailto:{email}")
    if name:
        addr.params["CN"] = vText(name)
    for key, value in params.items():
        addr.params[key] = vText(value)
    return addr


def build(event: IcsEvent) -> bytes:
    method = "CANCEL" if event.cancelled else "REQUEST"
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", method)
    vevent = Event()
    vevent.add("uid", event.uid)
    vevent.add("sequence", event.sequence)
    vevent.add("dtstamp", _utc(event.stamp or datetime.now(UTC)))
    vevent.add("dtstart", _utc(event.starts_at))
    vevent.add("dtend", _utc(event.ends_at))
    vevent.add("summary", event.summary)
    lines = [*event.description_lines, f"Time zone for this invitation: {event.recipient_timezone}"]
    if event.url and not any(event.url in line for line in lines):
        lines.insert(0, f"Join: {event.url}")
    vevent.add("description", "\n".join(lines))
    if event.location:
        vevent.add("location", event.location)
    if event.url:
        vevent.add("url", event.url)
    vevent.add("organizer", _address(event.organizer_email, event.organizer_name))
    for a in event.attendees:
        vevent.add(
            "attendee",
            _address(
                a.email,
                a.name,
                ROLE=a.role,
                PARTSTAT=PARTSTAT.get(a.response_status, "NEEDS-ACTION"),
                RSVP="TRUE" if not event.cancelled else "FALSE",
            ),
        )
    vevent.add("status", "CANCELLED" if event.cancelled else "CONFIRMED")
    vevent.add("transp", "OPAQUE")
    cal.add_component(vevent)
    return cal.to_ical()


def attachment(event: IcsEvent) -> EmailAttachment:
    method = "CANCEL" if event.cancelled else "REQUEST"
    return EmailAttachment(
        filename="invite.ics",
        content_type=f"text/calendar; method={method}; charset=UTF-8",
        data=build(event),
    )
