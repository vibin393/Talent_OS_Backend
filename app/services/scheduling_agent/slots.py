# ruff: file-ignore[line-too-long]
"""Pure slot engine (Epic 59 story 59.1, FR-297). No I/O, no clock: every input is passed in, so equal inputs give
equal outputs (hypothesis-tested).

A slot is valid when, for **every** participant, it lies inside one of their working-hour intervals (in their time zone)
and not on one of their holidays, and it lies inside the candidate's hours (in the candidate's time zone). It is
**free** when no participant with a connected calendar has a busy block overlapping it (the buffer is applied before
and after every busy block) and no participant is already at their ``max_per_day``. Participants without a calendar are
"availability unknown": their working hours and holidays still apply.

Candidates are generated on business days (Mon-Fri in the organiser's zone, skipping the organiser's holidays) at
``step_minutes`` boundaries of the organiser's local day. Selection spreads slots over days: at most ``per_day`` (2)
slots per day, one per day first. With no free slot, the best partial options (fewest busy participants) are returned
with ``partial=True``.
"""

import dataclasses
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Interval = tuple[str, str]


@dataclasses.dataclass(frozen=True, slots=True)
class Person:
    user_id: uuid.UUID
    timezone: str
    working_hours: Mapping[str, Sequence[Sequence[str]]]
    holidays: frozenset[date] = frozenset()
    busy: tuple[tuple[datetime, datetime], ...] = ()
    known: bool = True
    buffer_minutes: int = 0
    max_per_day: int | None = None
    booked_per_day: Mapping[date, int] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateSide:
    timezone: str
    start: str = "08:00"
    end: str = "21:00"


@dataclasses.dataclass(frozen=True, slots=True)
class SlotResult:
    start: datetime
    end: datetime
    conflicts: tuple[uuid.UUID, ...] = ()

    @property
    def all_known_free(self) -> bool:
        return not self.conflicts


@dataclasses.dataclass(frozen=True, slots=True)
class EngineResult:
    slots: tuple[SlotResult, ...]
    partial: bool
    unknown_participants: tuple[uuid.UUID, ...]


def _minutes(hhmm: str) -> int:
    if hhmm == "24:00":
        return 1440
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo("UTC")


def _local_minutes(dt_utc: datetime, tz: ZoneInfo) -> tuple[date, int]:
    local = dt_utc.astimezone(tz)
    return local.date(), local.hour * 60 + local.minute


def _inside_day(
    start: datetime, end: datetime, tz: ZoneInfo, intervals: Iterable[Sequence[str]]
) -> tuple[date, bool]:
    """Whether ``[start, end)`` lies inside one ``(from, to)`` interval of the local day of ``start``."""
    d0, m0 = _local_minutes(start, tz)
    d1, m1 = _local_minutes(end, tz)
    if d1 != d0:
        if d1 == d0 + timedelta(days=1) and m1 == 0:
            m1 = 1440
        else:
            return d0, False
    return d0, any(_minutes(a) <= m0 and m1 <= _minutes(b) for a, b in intervals)


def working_ok(person: Person, start: datetime, end: datetime) -> bool:
    tz = _zone(person.timezone)
    local_date = start.astimezone(tz).date()
    intervals = person.working_hours.get(WEEKDAY_KEYS[local_date.weekday()]) or ()
    _, ok = _inside_day(start, end, tz, intervals)
    return ok and local_date not in person.holidays


def candidate_ok(candidate: CandidateSide, start: datetime, end: datetime) -> bool:
    _, ok = _inside_day(start, end, _zone(candidate.timezone), ((candidate.start, candidate.end),))
    return ok


def busy_conflict(person: Person, start: datetime, end: datetime, buffer_minutes: int = 0) -> bool:
    buf = timedelta(minutes=max(buffer_minutes, person.buffer_minutes or 0))
    return any(b_start - buf < end and start < b_end + buf for b_start, b_end in person.busy)


def over_daily_cap(person: Person, start: datetime) -> bool:
    if person.max_per_day is None:
        return False
    local_date = start.astimezone(_zone(person.timezone)).date()
    return person.booked_per_day.get(local_date, 0) >= person.max_per_day


def evaluate(
    people: Sequence[Person],
    start: datetime,
    end: datetime,
    *,
    candidate: CandidateSide | None,
    buffer_minutes: int = 0,
) -> tuple[uuid.UUID, ...] | None:
    """``None`` when a hard rule fails (working hours, holiday, candidate hours); else the busy participants."""
    if candidate is not None and not candidate_ok(candidate, start, end):
        return None
    if not all(working_ok(p, start, end) for p in people):
        return None
    return tuple(
        p.user_id
        for p in people
        if p.known and (busy_conflict(p, start, end, buffer_minutes) or over_daily_cap(p, start))
    )


def business_days(first: date, count: int, holidays: frozenset[date]) -> list[date]:
    out: list[date] = []
    day = first
    while len(out) < count:
        if day.weekday() < 5 and day not in holidays:
            out.append(day)
        day += timedelta(days=1)
    return out


def horizon_window(
    earliest: datetime, business_tz: str, horizon_business_days: int, business_holidays: frozenset[date]
) -> tuple[datetime, datetime]:
    """UTC range covering the next ``horizon_business_days`` business days (for the free/busy lookup)."""
    tz = _zone(business_tz)
    days = business_days(earliest.astimezone(tz).date(), horizon_business_days, business_holidays)
    end_local = datetime.combine(days[-1] + timedelta(days=1), time(), tzinfo=tz)
    return earliest, end_local.astimezone(UTC) + timedelta(hours=14)  # + the widest zone offset


def _starts(day: date, tz: ZoneInfo, step: int) -> Iterable[datetime]:
    for minute in range(0, 1440, step):
        local = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=tz)
        utc = local.astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue  # DST gap: this wall time does not exist
        yield utc


def _spread(
    items: Sequence[SlotResult], business_tz: ZoneInfo, max_slots: int, per_day: int
) -> list[SlotResult]:
    by_day: dict[date, list[SlotResult]] = {}
    for s in items:
        by_day.setdefault(s.start.astimezone(business_tz).date(), []).append(s)
    chosen: dict[date, list[SlotResult]] = {d: [] for d in by_day}
    total = 0
    for round_no in range(per_day):
        for day in sorted(by_day):
            if total >= max_slots:
                break
            if len(chosen[day]) != round_no:
                continue
            taken = chosen[day]
            nxt = next(
                (
                    s
                    for s in by_day[day]
                    if all(s.start >= t.end or s.end <= t.start for t in taken) and s not in taken
                ),
                None,
            )
            if nxt is not None:
                taken.append(nxt)
                total += 1
    return sorted((s for day in chosen.values() for s in day), key=lambda s: s.start)


def compute(
    *,
    people: Sequence[Person],
    candidate: CandidateSide,
    duration_minutes: int,
    buffer_minutes: int,
    step_minutes: int,
    earliest: datetime,
    horizon_business_days: int,
    business_tz: str,
    business_holidays: frozenset[date] = frozenset(),
    max_slots: int = 5,
    per_day: int = 2,
) -> EngineResult:
    tz = _zone(business_tz)
    duration = timedelta(minutes=duration_minutes)
    unknown = tuple(p.user_id for p in people if not p.known)
    full: list[SlotResult] = []
    partial: list[SlotResult] = []
    for day in business_days(earliest.astimezone(tz).date(), horizon_business_days, business_holidays):
        for start in _starts(day, tz, step_minutes):
            if start < earliest:
                continue
            end = start + duration
            conflicts = evaluate(people, start, end, candidate=candidate, buffer_minutes=buffer_minutes)
            if conflicts is None:
                continue
            (partial if conflicts else full).append(SlotResult(start, end, conflicts))
    if full:
        return EngineResult(tuple(_spread(full, tz, max_slots, per_day)), False, unknown)
    if not partial:
        return EngineResult((), True, unknown)
    fewest = min(len(s.conflicts) for s in partial)
    best = [s for s in partial if len(s.conflicts) == fewest]
    return EngineResult(tuple(_spread(best, tz, max_slots, per_day)), True, unknown)


def match_windows(
    windows: Sequence[tuple[datetime, datetime]],
    *,
    people: Sequence[Person],
    duration_minutes: int,
    buffer_minutes: int,
    step_minutes: int,
    earliest: datetime,
) -> tuple[list[SlotResult], list[SlotResult]]:
    """Candidate-proposed windows: the free slots inside them (``matches``) and the valid-but-busy ones (``conflicts``).
    The candidate's own hours do not apply (they proposed the window)."""
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    matches: list[SlotResult] = []
    busy: list[SlotResult] = []
    for w_start, w_end in windows:
        start = w_start.astimezone(UTC)
        if start.minute % step_minutes or start.second:
            start = start.replace(second=0, microsecond=0) + timedelta(
                minutes=(step_minutes - start.minute % step_minutes) % step_minutes
            )
        while start + duration <= w_end:
            if start >= earliest:
                conflicts = evaluate(
                    people, start, start + duration, candidate=None, buffer_minutes=buffer_minutes
                )
                if conflicts is not None:
                    (busy if conflicts else matches).append(SlotResult(start, start + duration, conflicts))
            start += step
    matches.sort(key=lambda s: s.start)
    busy.sort(key=lambda s: s.start)
    return matches, busy


__all__ = [
    "CandidateSide",
    "EngineResult",
    "Person",
    "SlotResult",
    "business_days",
    "busy_conflict",
    "candidate_ok",
    "compute",
    "evaluate",
    "horizon_window",
    "match_windows",
    "working_ok",
]
