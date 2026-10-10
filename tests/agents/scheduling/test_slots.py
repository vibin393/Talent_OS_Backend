# ruff: noqa: E501
"""Epic 59 story 59.1: the pure slot engine (table + hypothesis tests). No database, no clock."""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.scheduling_agent import slots
from app.services.scheduling_agent.slots import CandidateSide, Person

WEEK = {d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
ALL_DAY = {d: [["00:00", "24:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
A, B = uuid.UUID(int=1), uuid.UUID(int=2)
MONDAY = datetime(2031, 1, 6, 0, 0, tzinfo=UTC)  # a Monday


def _compute(people: list[Person], **kw: object) -> slots.EngineResult:
    args: dict[str, object] = {
        "people": people, "candidate": CandidateSide("Asia/Kolkata"), "duration_minutes": 60, "buffer_minutes": 15,
        "step_minutes": 30, "earliest": MONDAY, "horizon_business_days": 10, "business_tz": "Asia/Kolkata", "max_slots": 5,
    }  # fmt: skip
    args.update(kw)
    return slots.compute(**args)  # type: ignore[arg-type]


def test_basic_slots_inside_hours_and_spread_over_days() -> None:
    res = _compute([Person(A, "Asia/Kolkata", WEEK)])
    assert not res.partial and len(res.slots) == 5
    days = [s.start.astimezone(ZoneInfo("Asia/Kolkata")).date() for s in res.slots]
    assert len(set(days)) == 5  # one per day first
    for s in res.slots:
        local = s.start.astimezone(ZoneInfo("Asia/Kolkata"))
        assert (
            local.weekday() < 5
            and local.hour >= 9
            and (s.end.astimezone(ZoneInfo("Asia/Kolkata")).hour, s.end.minute) <= (18, 0)
        )
    assert [s.start for s in res.slots] == sorted(s.start for s in res.slots)


def test_at_most_two_per_day() -> None:
    res = _compute([Person(A, "Asia/Kolkata", WEEK)], horizon_business_days=3, max_slots=10)
    days: dict[date, int] = {}
    for s in res.slots:
        d = s.start.astimezone(ZoneInfo("Asia/Kolkata")).date()
        days[d] = days.get(d, 0) + 1
    assert max(days.values()) <= 2 and len(res.slots) == 6


def test_busy_blocks_with_buffer_are_avoided() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    busy = []
    for i in range(
        14
    ):  # busy 09:00-17:00 every day: only 17:15+ remains (with 15 min buffer) -> nothing fits by 18:00
        d = (MONDAY + timedelta(days=i)).astimezone(tz).date()
        busy.append(
            (
                datetime(d.year, d.month, d.day, 9, 0, tzinfo=tz).astimezone(UTC),
                datetime(d.year, d.month, d.day, 17, 0, tzinfo=tz).astimezone(UTC),
            )
        )
    res = _compute([Person(A, "Asia/Kolkata", WEEK, busy=tuple(busy))])
    assert res.partial and all(not s.all_known_free for s in res.slots)


def test_partial_prefers_fewest_busy_participants() -> None:
    always = ((MONDAY - timedelta(days=1), MONDAY + timedelta(days=40)),)
    res = _compute([Person(A, "Asia/Kolkata", WEEK, busy=always), Person(B, "Asia/Kolkata", WEEK)])
    assert res.partial and res.slots and all(s.conflicts == (A,) for s in res.slots)


def test_unknown_participant_does_not_block_but_is_listed() -> None:
    busy = ((MONDAY - timedelta(days=1), MONDAY + timedelta(days=40)),)
    res = _compute([Person(A, "Asia/Kolkata", WEEK), Person(B, "Asia/Kolkata", WEEK, busy=busy, known=False)])
    assert not res.partial and res.unknown_participants == (B,)


def test_ist_candidate_with_us_interviewer() -> None:
    """An IST candidate (08:00-21:00) and a New York interviewer (09:00-18:00 ET): only the overlap is offered."""
    res = _compute([Person(A, "America/New_York", WEEK)], business_tz="America/New_York")
    assert res.slots
    for s in res.slots:
        ny = s.start.astimezone(ZoneInfo("America/New_York"))
        ist_end = s.end.astimezone(ZoneInfo("Asia/Kolkata"))
        assert (
            ny.hour >= 9
            and ist_end.hour * 60 + ist_end.minute <= 21 * 60
            and ist_end.date() == s.start.astimezone(ZoneInfo("Asia/Kolkata")).date()
        )


def test_holidays_and_horizon() -> None:
    first = MONDAY.date()
    res = _compute(
        [Person(A, "Asia/Kolkata", WEEK, holidays=frozenset({first}))],
        horizon_business_days=3,
        business_holidays=frozenset({first + timedelta(days=1)}),
    )
    days = {s.start.astimezone(ZoneInfo("Asia/Kolkata")).date() for s in res.slots}
    assert first not in days and first + timedelta(days=1) not in days
    assert max(days) <= first + timedelta(
        days=3
    )  # Wed, Thu, (Mon/Tue excluded) -> within the 3 business days


def test_max_interviews_per_day() -> None:
    first = MONDAY.date()
    res = _compute(
        [Person(A, "Asia/Kolkata", WEEK, max_per_day=2, booked_per_day={first: 2})], horizon_business_days=1
    )
    assert res.partial  # Monday is full: only partial options


def test_dst_gap_is_skipped() -> None:
    # 2031-03-09: US DST starts (02:00 -> 03:00); an all-day interviewer never gets a 02:30 local slot.
    earliest = datetime(2031, 3, 7, 0, 0, tzinfo=UTC)
    res = slots.compute(
        people=[Person(A, "America/New_York", ALL_DAY)], candidate=CandidateSide("America/New_York", "00:00", "24:00"),
        duration_minutes=30, buffer_minutes=0, step_minutes=30, earliest=earliest, horizon_business_days=3,
        business_tz="America/New_York", max_slots=10,
    )  # fmt: skip
    for s in res.slots:
        local = s.start.astimezone(ZoneInfo("America/New_York"))
        assert (local.date(), local.hour) != (date(2031, 3, 9), 2)


def test_match_windows() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    start = datetime(2031, 1, 7, 15, 0, tzinfo=tz)
    matches, busy = slots.match_windows(
        [(start, start + timedelta(hours=2))], people=[Person(A, "Asia/Kolkata", WEEK)], duration_minutes=60,
        buffer_minutes=0, step_minutes=30, earliest=MONDAY,
    )  # fmt: skip
    assert [m.start for m in matches] == [
        start,
        start + timedelta(minutes=30),
        start + timedelta(minutes=60),
    ] and busy == []
    late = datetime(2031, 1, 7, 19, 0, tzinfo=tz)  # outside working hours
    matches, _ = slots.match_windows(
        [(late, late + timedelta(hours=1))],
        people=[Person(A, "Asia/Kolkata", WEEK)],
        duration_minutes=60,
        buffer_minutes=0,
        step_minutes=30,
        earliest=MONDAY,
    )
    assert matches == []


busy_blocks = st.lists(
    st.tuples(st.integers(0, 14 * 24 * 4), st.integers(1, 16)).map(
        lambda t: (MONDAY + timedelta(minutes=15 * t[0]), MONDAY + timedelta(minutes=15 * (t[0] + t[1])))
    ),
    max_size=20,
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    busy=busy_blocks,
    buffer=st.sampled_from([0, 15, 30]),
    tz=st.sampled_from(["Asia/Kolkata", "Europe/London", "America/New_York"]),
)
def test_property_never_overlaps_busy_and_stays_in_hours(
    busy: list[tuple[datetime, datetime]], buffer: int, tz: str
) -> None:
    person = Person(A, tz, WEEK, busy=tuple(busy))
    cand = CandidateSide("Asia/Kolkata")
    res = _compute([person], buffer_minutes=buffer, candidate=cand, business_tz=tz)
    again = _compute([person], buffer_minutes=buffer, candidate=cand, business_tz=tz)
    assert res == again  # deterministic
    for s in res.slots:
        assert slots.working_ok(person, s.start, s.end)
        assert slots.candidate_ok(cand, s.start, s.end)
        if s.all_known_free:
            assert not any(
                b0 - timedelta(minutes=buffer) < s.end and s.start < b1 + timedelta(minutes=buffer)
                for b0, b1 in busy
            )
