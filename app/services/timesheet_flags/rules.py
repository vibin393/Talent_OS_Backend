# ruff: noqa: E501, D401
"""Deterministic timesheet anomaly rules (Epic 63 A1, FR-312). Pure: plain dataclasses in, ``FlagHit`` out; no I/O,
no AI. Flags never block approval and never change hours.

| Rule | Fires when | Severity |
|---|---|---|
| ``hours_on_leave`` | work hours on an approved full-day leave, or more than 50 % of a standard day on a half day | warning |
| ``holiday_work`` | work hours on a counted holiday of the placement calendar (``regular`` -> warning, OT / DT only -> info) | info / warning |
| ``weekend_work`` | work hours on a non-work day (config ``work_days``) that is not a holiday | info |
| ``daily_hours_vs_schedule`` | a day >= 1.5 x the standard day but within ``daily_cap`` (P2 blocks above the cap) | info |
| ``weekly_overtime_vs_history`` | OT + DT >= 3 x the median of the last 8 approved periods (median >= 1 h), or >= 10 h otherwise | warning |
| ``peer_outlier`` | total > peer median + max(2 x MAD, 1 h), with >= 3 peers on the same client (and project) | info |
| ``po_hours_cap`` | the timesheet's billable hours exceed the remaining PO ``hours_cap`` | warning |
| ``zero_hours_period`` | zero hours on a period with work days and no leave or holiday to explain it | info |
| ``duplicate_entry_pattern`` | identical hours and (non-empty) note on >= 10 consecutive work days | info |

``facts`` carry numbers (and ids) only -- never notes or names -- so explanations can be checked against them.
"""

import dataclasses
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

WORK_CATEGORIES = frozenset({"regular", "ot", "double_time"})
OT_CATEGORIES = frozenset({"ot", "double_time"})
LONG_DAY_FACTOR = Decimal("1.5")
OT_HISTORY_FACTOR = Decimal(3)
OT_NO_HISTORY_HOURS = Decimal(10)
PEER_MIN = 3
PEER_FLOOR = Decimal(1)
DUPLICATE_RUN = 10
HALF = Decimal("0.5")
ZERO = Decimal(0)

LABELS: dict[str, str] = {
    "hours_on_leave": "Hours on approved leave",
    "holiday_work": "Work on a public holiday",
    "weekend_work": "Weekend work",
    "daily_hours_vs_schedule": "Long day",
    "weekly_overtime_vs_history": "Overtime well above usual",
    "peer_outlier": "Hours above project peers",
    "po_hours_cap": "PO hours cap",
    "zero_hours_period": "Zero hours",
    "duplicate_entry_pattern": "Repeated identical entries",
}


@dataclasses.dataclass(frozen=True, slots=True)
class Entry:
    day: date
    category: str
    hours: Decimal
    note: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FlagInput:
    period_start: date
    period_end: date
    entries: tuple[Entry, ...]
    work_days: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    hours_per_day: Decimal = Decimal(8)
    daily_cap: Decimal = Decimal(24)
    leave: Mapping[date, Decimal] = dataclasses.field(default_factory=dict)  # approved leave fraction (1 or 0.5)
    holidays: Mapping[date, str] = dataclasses.field(default_factory=dict)  # date -> holiday type
    history_ot: tuple[Decimal, ...] = ()  # OT + DT of the last approved periods (<= 8)
    peer_totals: tuple[Decimal, ...] = ()
    po_remaining_hours: Decimal | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FlagHit:
    rule: str
    severity: str
    entry_dates: tuple[date, ...]
    facts: dict[str, Any]


def _num(value: Decimal) -> float | int:
    q = value.quantize(Decimal("0.01"))
    return int(q) if q == q.to_integral_value() else float(q)


def _days(inp: FlagInput) -> list[date]:
    from datetime import timedelta

    out: list[date] = []
    d = inp.period_start
    while d <= inp.period_end:
        out.append(d)
        d += timedelta(days=1)
    return out


def _work_by_day(entries: Iterable[Entry], categories: frozenset[str] = WORK_CATEGORIES) -> dict[date, Decimal]:
    out: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for e in entries:
        if e.category in categories and e.hours > 0:
            out[e.day] += e.hours
    return dict(out)


def median(values: Sequence[Decimal]) -> Decimal:
    return Decimal(str(statistics.median(values))) if values else ZERO


def mad(values: Sequence[Decimal]) -> Decimal:
    m = median(values)
    return median([abs(v - m) for v in values]) if values else ZERO


# ---------------------------------------------------------------- the rules
def hours_on_leave(inp: FlagInput, work: Mapping[date, Decimal]) -> FlagHit | None:
    days: list[date] = []
    hours = ZERO
    for d, fraction in sorted(inp.leave.items()):
        h = work.get(d, ZERO)
        if h <= 0:
            continue
        if fraction >= 1 or (fraction > 0 and h > inp.hours_per_day * HALF):
            days.append(d)
            hours += h
    if not days:
        return None
    return FlagHit("hours_on_leave", "warning", tuple(days), {"days": len(days), "hours": _num(hours)})


def holiday_work(inp: FlagInput) -> FlagHit | None:
    regular = _work_by_day(inp.entries, frozenset({"regular"}))
    any_work = _work_by_day(inp.entries)
    days = sorted(d for d in inp.holidays if any_work.get(d, ZERO) > 0)
    if not days:
        return None
    hours = sum((any_work[d] for d in days), ZERO)
    severity = "warning" if any(regular.get(d, ZERO) > 0 for d in days) else "info"
    return FlagHit("holiday_work", severity, tuple(days), {"days": len(days), "hours": _num(hours)})


def weekend_work(inp: FlagInput, work: Mapping[date, Decimal]) -> FlagHit | None:
    days = sorted(d for d, h in work.items() if h > 0 and d.weekday() not in inp.work_days and d not in inp.holidays)
    if not days:
        return None
    hours = sum((work[d] for d in days), ZERO)
    return FlagHit("weekend_work", "info", tuple(days), {"days": len(days), "hours": _num(hours)})


def daily_hours_vs_schedule(inp: FlagInput, work: Mapping[date, Decimal]) -> FlagHit | None:
    threshold = inp.hours_per_day * LONG_DAY_FACTOR
    days = sorted(d for d, h in work.items() if threshold <= h <= inp.daily_cap)
    if not days or inp.hours_per_day <= 0:
        return None
    longest = max(work[d] for d in days)
    return FlagHit(
        "daily_hours_vs_schedule", "info", tuple(days),
        {"days": len(days), "longest_day_hours": _num(longest), "standard_day_hours": _num(inp.hours_per_day)},
    )  # fmt: skip


def weekly_overtime_vs_history(inp: FlagInput) -> FlagHit | None:
    ot_by_day = _work_by_day(inp.entries, OT_CATEGORIES)
    ot = sum(ot_by_day.values(), ZERO)
    if ot <= 0:
        return None
    history = list(inp.history_ot[:8])
    med = median(history)
    if med >= 1:
        if ot < med * OT_HISTORY_FACTOR:
            return None
    elif ot < OT_NO_HISTORY_HOURS:
        return None
    return FlagHit(
        "weekly_overtime_vs_history", "warning", tuple(sorted(ot_by_day)),
        {"overtime_hours": _num(ot), "usual_overtime_hours": _num(med), "periods_compared": len(history)},
    )  # fmt: skip


def peer_outlier(inp: FlagInput, total: Decimal) -> FlagHit | None:
    peers = list(inp.peer_totals)
    if len(peers) < PEER_MIN:
        return None
    med, spread = median(peers), mad(peers)
    limit = med + max(spread * 2, PEER_FLOOR)
    if total <= limit:
        return None
    return FlagHit(
        "peer_outlier", "info", (),
        {"total_hours": _num(total), "peer_median_hours": _num(med), "peers": len(peers)},
    )  # fmt: skip


def po_hours_cap(inp: FlagInput, billable: Decimal) -> FlagHit | None:
    if inp.po_remaining_hours is None or billable <= inp.po_remaining_hours:
        return None
    return FlagHit(
        "po_hours_cap", "warning", (),
        {"timesheet_hours": _num(billable), "po_remaining_hours": _num(max(inp.po_remaining_hours, ZERO))},
    )  # fmt: skip


def zero_hours_period(inp: FlagInput, total: Decimal) -> FlagHit | None:
    if total > 0:
        return None
    work_days = [d for d in _days(inp) if d.weekday() in inp.work_days]
    if not work_days or any(d in inp.leave or d in inp.holidays for d in work_days):
        return None
    return FlagHit("zero_hours_period", "info", (), {"work_days": len(work_days), "hours": 0})


def duplicate_entry_pattern(inp: FlagInput) -> FlagHit | None:
    by_day: dict[date, list[Entry]] = defaultdict(list)
    for e in inp.entries:
        if e.category in WORK_CATEGORIES and e.hours > 0:
            by_day[e.day].append(e)
    work_days = [d for d in _days(inp) if d.weekday() in inp.work_days]
    best: list[date] = []
    run: list[date] = []
    last_key: tuple[Decimal, str] | None = None
    for d in work_days:
        rows = by_day.get(d, [])
        note = (rows[0].note or "").strip().lower() if len(rows) == 1 else ""
        key = (rows[0].hours, note) if len(rows) == 1 and note else None
        if key is not None and key == last_key:
            run.append(d)
        else:
            run = [d] if key is not None else []
        last_key = key
        if len(run) > len(best):
            best = list(run)
    if len(best) < DUPLICATE_RUN:
        return None
    return FlagHit(
        "duplicate_entry_pattern", "info", tuple(best),
        {"consecutive_days": len(best), "hours_per_day": _num(by_day[best[0]][0].hours)},
    )  # fmt: skip


def evaluate(inp: FlagInput) -> list[FlagHit]:
    """Every rule hit for one timesheet version, in a stable order."""
    work = _work_by_day(inp.entries)
    total = sum((e.hours for e in inp.entries if e.hours > 0), ZERO)
    work_total = sum(work.values(), ZERO)
    hits = (
        hours_on_leave(inp, work),
        holiday_work(inp),
        weekend_work(inp, work),
        daily_hours_vs_schedule(inp, work),
        weekly_overtime_vs_history(inp),
        peer_outlier(inp, work_total),
        po_hours_cap(inp, work_total),
        zero_hours_period(inp, total),
        duplicate_entry_pattern(inp),
    )
    return [h for h in hits if h is not None]


# ---------------------------------------------------------------- explanations
def _fmt(value: Any) -> str:
    return str(value)


def describe(rule: str, facts: Mapping[str, Any]) -> str:
    """The rules-only description shown when no AI explanation exists (AI or the agent off)."""
    f = {k: _fmt(v) for k, v in facts.items()}
    texts = {
        "hours_on_leave": "{hours} hours logged on {days} day(s) of approved leave.",
        "holiday_work": "{hours} hours logged on {days} public holiday(s).",
        "weekend_work": "{hours} hours logged on {days} non-working day(s).",
        "daily_hours_vs_schedule": "{days} day(s) of at least 1.5 times the standard {standard_day_hours}-hour day (longest {longest_day_hours} hours).",
        "weekly_overtime_vs_history": "{overtime_hours} overtime hours against a usual {usual_overtime_hours} hours over the last {periods_compared} approved period(s).",
        "peer_outlier": "{total_hours} hours against a median of {peer_median_hours} hours for {peers} peers on the same client.",
        "po_hours_cap": "{timesheet_hours} hours exceed the {po_remaining_hours} hours left on the purchase order.",
        "zero_hours_period": "No hours logged across {work_days} working day(s) with no leave or holiday.",
        "duplicate_entry_pattern": "The same {hours_per_day} hours and note were entered on {consecutive_days} consecutive working days.",
    }
    try:
        return texts[rule].format_map(f)
    except (KeyError, IndexError):
        return LABELS.get(rule, rule)


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _norm(token: str) -> str:
    try:
        return format(Decimal(token.replace(",", "")).normalize(), "f")
    except Exception:  # pragma: no cover - regex guarantees digits
        return token


def allowed_numbers(facts: Mapping[str, Any], entry_dates: Iterable[date], extra: Iterable[Any] = ()) -> set[str]:
    out: set[str] = set()
    for v in [*facts.values(), *extra]:
        if isinstance(v, bool):
            continue
        if isinstance(v, int | float | Decimal):
            out.add(_norm(str(v)))
        elif isinstance(v, str):
            out.update(_norm(t) for t in _NUMBER.findall(v))
    for d in entry_dates:
        out.update({str(d.day), str(d.month), str(d.year), f"{d.day:02d}", f"{d.month:02d}"})
    out.update({"1.5", "2", "3", "10", "50"})  # rule thresholds the explanation may quote
    return {_norm(x) for x in out}


def unknown_numbers(text: str, facts: Mapping[str, Any], entry_dates: Iterable[date]) -> list[str]:
    """Numbers in ``text`` that are not in the flag facts / dates (pure check of ``time.save_flag_explanations``)."""
    allowed = allowed_numbers(facts, entry_dates)
    return sorted({t for t in _NUMBER.findall(text) if _norm(t) not in allowed})


__all__ = [
    "LABELS",
    "OT_CATEGORIES",
    "WORK_CATEGORIES",
    "Entry",
    "FlagHit",
    "FlagInput",
    "allowed_numbers",
    "describe",
    "evaluate",
    "mad",
    "median",
    "unknown_numbers",
]
