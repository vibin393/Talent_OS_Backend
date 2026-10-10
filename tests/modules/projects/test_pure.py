# ruff: noqa: E501
"""Epic 45 pure cores: the resource timeline (gaps / over-allocation) and the burn computation (allocation
split, window attribution, credit notes, fx conversion, deterministic hash)."""

import uuid
from datetime import date
from decimal import Decimal

from app.services.projects.burn import Fact, Window, compute
from app.services.projects.timeline import Span, role_segments, window, worker_segments

D = Decimal
P1, P2 = uuid.uuid4(), uuid.uuid4()


def test_role_segments_gap_and_merge() -> None:
    spans = [Span(date(2026, 3, 1), date(2026, 3, 31), D("100")), Span(date(2026, 3, 1), None, D("50"))]
    segs = role_segments(date(2026, 2, 1), date(2026, 4, 30), 2, spans, role_start=date(2026, 2, 15))
    assert [(s.start, s.end, s.assigned_fte, s.gap) for s in segs] == [
        (date(2026, 2, 15), date(2026, 2, 28), D("0.00"), True),
        (date(2026, 3, 1), date(2026, 3, 31), D("1.50"), True),
        (date(2026, 4, 1), date(2026, 4, 30), D("0.50"), True),
    ]
    full = role_segments(date(2026, 3, 1), date(2026, 3, 10), 1, [Span(date(2026, 1, 1), None, D("100"))])
    assert len(full) == 1 and not full[0].gap and full[0].assigned_fte == D("1.00")
    assert role_segments(date(2026, 1, 1), date(2026, 1, 31), 1, [], role_start=date(2026, 3, 1)) == []


def test_worker_over_allocation_110() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    spans = [
        Span(date(2026, 3, 1), None, D("60"), a, "Project A"),
        Span(date(2026, 3, 15), date(2026, 3, 20), D("50"), b, "Project B"),
    ]
    segs = worker_segments(date(2026, 3, 1), date(2026, 3, 31), spans)
    assert [(s.start, s.end, s.total_pct, s.over) for s in segs] == [
        (date(2026, 3, 1), date(2026, 3, 14), D("60"), False),
        (date(2026, 3, 15), date(2026, 3, 20), D("110"), True),
        (date(2026, 3, 21), date(2026, 3, 31), D("60"), False),
    ]
    assert {x.project_name for x in segs[1].split} == {"Project A", "Project B"}


def test_window_defaults_and_cap() -> None:
    assert window(None, None, date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 4, 1))
    lo, hi = window(date(2026, 1, 1), date(2030, 1, 1), date(2026, 1, 1))
    assert (hi - lo).days == 731


WINDOWS = [Window(P1, date(2026, 1, 1), date(2026, 3, 31), D("50")), Window(P2, date(2026, 2, 1), None, D("100"))]


def _facts() -> list[Fact]:
    return [
        Fact("revenue", P1, date(2026, 1, 31), D("1000.00"), "INR", "invoice_line"),
        Fact("revenue", P1, date(2026, 4, 30), D("999.00"), "INR", "invoice_line"),  # outside P1's window
        Fact("revenue", P2, date(2026, 2, 28), D("100.00"), "USD", "invoice_line"),
        Fact("revenue", P2, date(2026, 2, 28), D("-20.00"), "USD", "credit_note_line"),
        Fact("revenue", None, date(2026, 3, 15), D("5000.00"), "INR", "invoice_line", True),  # milestone line
        Fact("hours", P1, date(2026, 1, 5), D("8"), None, "timesheet"),
        Fact("cost", P1, date(2026, 1, 31), D("400.00"), "INR", "payable_posting"),
    ]


def test_burn_allocation_window_credit_and_fx() -> None:
    rates = {("USD", "INR", date(2026, 2, 28)): D("83.5")}
    fig = compute(
        "revenue", _facts(), WINDOWS, currency="INR", rates=rates, budget=D("10000"),
        start=date(2026, 1, 1), end=date(2026, 12, 31), as_of=date(2026, 3, 31),
    )
    # 1000 * 50% + (100 - 20) USD * 83.5 + 5000 (milestone, window-free)
    assert fig.actual == D("500.00") + D("6680.00") + D("5000.00")
    assert fig.pct == D("121.8000") and fig.currency == "INR"
    assert fig.breakdown["fx"] == {"USD>INR@2026-02-28": "83.5"}
    assert fig.burn_rate_per_day == (fig.actual / D(90)).quantize(D("0.01"))
    assert fig.forecast_at_completion == (fig.actual + fig.burn_rate_per_day * D(275)).quantize(D("0.01"))
    hours = compute("hours", _facts(), WINDOWS, currency="INR", rates=rates, budget=None, start=None, end=None, as_of=date(2026, 3, 31))
    assert hours.actual == D("4.00") and hours.currency is None and hours.pct is None
    cost = compute("cost", _facts(), WINDOWS, currency="INR", rates=rates, budget=D("200"), start=None, end=None, as_of=date(2026, 3, 31))
    assert cost.actual == D("200.00") and cost.pct == D("100.0000")


def test_burn_missing_fx_is_flagged_and_hash_is_deterministic() -> None:
    a = compute("revenue", _facts(), WINDOWS, currency="INR", rates={}, budget=None, start=None, end=None, as_of=date(2026, 3, 31))
    b = compute("revenue", list(reversed(_facts())), WINDOWS, currency="INR", rates={}, budget=None, start=None, end=None, as_of=date(2026, 3, 31))
    assert a.breakdown["fx_missing"] == ["USD>INR@2026-02-28"]
    assert a.actual == D("5500.00") and a.source_hash == b.source_hash and a.breakdown == b.breakdown
    c = compute("revenue", _facts()[:1], WINDOWS, currency="INR", rates={}, budget=None, start=None, end=None, as_of=date(2026, 3, 31))
    assert c.source_hash != a.source_hash
