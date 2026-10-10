# ruff: noqa: E501
"""Epic 63 pure units: the deterministic anomaly rules (tables + hypothesis), the rules-only descriptions, the
explanation number check and the nudge planner."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.timesheet_flags import nudges, rules
from app.services.timesheet_flags.rules import Entry, FlagInput

MON = date(2026, 9, 7)  # a Monday
D = Decimal


def week(*hours: str, category: str = "regular", start: date = MON, note: str | None = None) -> tuple[Entry, ...]:
    return tuple(Entry(start + timedelta(days=i), category, D(h), note) for i, h in enumerate(hours) if D(h) > 0)


def inp(entries: tuple[Entry, ...] = (), days: int = 7, **kw: object) -> FlagInput:
    return FlagInput(period_start=MON, period_end=MON + timedelta(days=days - 1), entries=entries, **kw)  # type: ignore[arg-type]


def hit_rules(i: FlagInput) -> dict[str, rules.FlagHit]:
    return {h.rule: h for h in rules.evaluate(i)}


# ================================================================ table
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (inp(week("8", "8", "8", "8", "8")), {}),
        (inp(week("8", "9", "8", "8", "8"), leave={MON + timedelta(days=1): D(1)}), {"hours_on_leave": "warning"}),
        (inp(week("8", "3", "8", "8", "8"), leave={MON + timedelta(days=1): D("0.5")}), {}),
        (inp(week("8", "5", "8", "8", "8"), leave={MON + timedelta(days=1): D("0.5")}), {"hours_on_leave": "warning"}),
        (inp(week("8", "8", "8", "8", "8"), holidays={MON: "national"}), {"holiday_work": "warning"}),
        (inp(week("8", "8", "8", "8", "8", "4")), {"weekend_work": "info"}),
        (inp(week("8", "12", "8", "8", "8")), {"daily_hours_vs_schedule": "info"}),
        (inp(week("8", "8", "8", "8", "8") + week("3", "3", "3", category="ot"), history_ot=(D(2), D(2), D(4))), {"weekly_overtime_vs_history": "warning"}),
        (inp(week("8", "8", "8", "8", "8") + week("2", "2", category="ot"), history_ot=(D(2), D(2), D(4))), {}),
        (inp(week("8", "8", "8", "8", "8") + week("3", "3", "2", "2", category="ot")), {"weekly_overtime_vs_history": "warning"}),
        (inp(week("8", "8", "8", "8", "8"), peer_totals=(D(30), D(31), D(30))), {"peer_outlier": "info"}),
        (inp(week("8", "8", "8", "8", "8"), peer_totals=(D(40), D(41))), {}),
        (inp(week("8", "8", "8", "8", "8"), po_remaining_hours=D(30)), {"po_hours_cap": "warning"}),
        (inp(()), {"zero_hours_period": "info"}),
        (inp((), leave={MON: D(1)}), {}),
    ],
)
def test_rule_table(case: FlagInput, expected: dict[str, str]) -> None:
    got = {r: h.severity for r, h in hit_rules(case).items()}
    assert got == expected, got


def test_duplicate_entry_pattern_needs_ten_identical_noted_days() -> None:
    entries = tuple(
        Entry(MON + timedelta(days=i), "regular", D(8), "Project Atlas support")
        for i in range(14)
        if (MON + timedelta(days=i)).weekday() < 5
    )
    got = hit_rules(inp(entries, days=14))
    assert got["duplicate_entry_pattern"].facts == {"consecutive_days": 10, "hours_per_day": 8}
    no_notes = tuple(Entry(e.day, e.category, e.hours, None) for e in entries)
    assert "duplicate_entry_pattern" not in hit_rules(inp(no_notes, days=14))


def test_holiday_overtime_only_is_info() -> None:
    got = hit_rules(inp(week("8", category="ot"), holidays={MON: "national"}))
    assert got["holiday_work"].severity == "info"


def test_facts_are_numbers_only() -> None:
    i = inp(week("8", "9", "12", "8", "8", "4") + week("4", "4", "4", category="ot"), leave={MON + timedelta(days=1): D(1)},
            holidays={MON: "state"}, peer_totals=(D(10), D(10), D(10)), po_remaining_hours=D(5))  # fmt: skip
    for h in rules.evaluate(i):
        assert all(isinstance(v, int | float) for v in h.facts.values()), h
        assert rules.describe(h.rule, h.facts) != rules.LABELS[h.rule]


# ================================================================ explanation number check
def test_unknown_numbers_are_refused() -> None:
    facts = {"days": 1, "hours": 9}
    dates = [date(2026, 9, 8)]
    assert rules.unknown_numbers("9 hours were logged on approved leave on 8 Sep 2026.", facts, dates) == []
    assert rules.unknown_numbers("9 hours were logged; that is 40 hours over budget.", facts, dates) == ["40"]
    assert rules.unknown_numbers("9.0 hours on 1 day.", facts, dates) == []


# ================================================================ hypothesis: flags never depend on order; never negative
hours_st = st.lists(st.decimals(min_value=0, max_value=16, places=1), min_size=7, max_size=7)


@settings(max_examples=60, deadline=None)
@given(hours=hours_st, leave_day=st.integers(min_value=0, max_value=6), peers=st.lists(st.integers(0, 60), max_size=6))
def test_rules_are_order_independent_and_well_formed(hours: list[Decimal], leave_day: int, peers: list[int]) -> None:
    entries = tuple(Entry(MON + timedelta(days=i), "regular", h, None) for i, h in enumerate(hours) if h > 0)
    base: dict[str, object] = {"leave": {MON + timedelta(days=leave_day): D(1)}, "peer_totals": tuple(D(p) for p in peers)}
    a = rules.evaluate(inp(entries, **base))
    b = rules.evaluate(inp(tuple(reversed(entries)), **base))
    assert [(h.rule, h.severity, h.facts) for h in a] == [(h.rule, h.severity, h.facts) for h in b]
    for h in a:
        assert h.rule in rules.LABELS and h.severity in ("info", "warning")
        assert all(MON <= d <= MON + timedelta(days=6) for d in h.entry_dates)
    if any(h > 0 for h in hours):
        assert "zero_hours_period" not in {h.rule for h in a}


# ================================================================ nudge planner
TODAY = date(2026, 9, 17)  # Thursday
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=UTC)
AM = uuid.uuid4()


def fact(**kw: object) -> nudges.NudgeFacts:
    base: dict[str, object] = {
        "timesheet_id": uuid.uuid4(), "human_id": "TS-1", "status": "draft", "period_start": date(2026, 9, 7),
        "period_end": date(2026, 9, 13), "placement_id": uuid.uuid4(), "candidate_id": uuid.uuid4(),
        "recipient_kind": "worker", "recipient_id": uuid.uuid4(),
    }  # fmt: skip
    base.update(kw)
    return nudges.NudgeFacts(**base)  # type: ignore[arg-type]


def test_submit_nudge_after_p2_days_and_not_within_20h() -> None:
    late = fact()  # period ended Sun 13th -> 4 business days
    fresh = fact(period_end=date(2026, 9, 16))  # 1 business day: P2 still covers it
    recent = fact(last_reminder_at=NOW - timedelta(hours=5))  # P2 reminded today
    plan = nudges.plan(AM, [late, fresh, recent], TODAY, NOW)
    assert [i.timesheet_id for i in plan.submit] == [late.timesheet_id]


def test_submit_cadence_and_max_then_task() -> None:
    every = fact(agent_nudges=1, last_agent_nudge_on=TODAY - timedelta(days=1))
    due = fact(agent_nudges=2, last_agent_nudge_on=TODAY - timedelta(days=3))
    maxed = fact(agent_nudges=nudges.SUBMIT_MAX, last_agent_nudge_on=TODAY - timedelta(days=5))
    plan = nudges.plan(AM, [every, due, maxed], TODAY, NOW)
    assert [i.timesheet_id for i in plan.submit] == [due.timesheet_id]
    assert [i.timesheet_id for i in plan.max_reached] == [maxed.timesheet_id]


def test_approve_nudge_and_escalation_once_per_step() -> None:
    client_step = fact(status="in_approval", recipient_kind="client_user", step_since=TODAY - timedelta(days=6), step_key="r:2")
    escalated = fact(status="submitted", recipient_kind="user", step_since=TODAY - timedelta(days=6), escalated=True)
    young = fact(status="submitted", recipient_kind="user", step_since=TODAY - timedelta(days=1))
    plan = nudges.plan(AM, [client_step, escalated, young], TODAY, NOW)
    assert {i.timesheet_id for i in plan.approve} == {client_step.timesheet_id, escalated.timesheet_id}
    assert [i.timesheet_id for i in plan.escalate] == [client_step.timesheet_id]
    assert next(i for i in plan.approve if i.timesheet_id == client_step.timesheet_id).recipient_kind == "client_user"
