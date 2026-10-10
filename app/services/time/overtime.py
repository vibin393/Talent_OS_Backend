"""Pure OT / double-time splitter (FR-129, AD-20). Decimal only; inputs carry <= 2 dp so outputs are exact.

1. Per day: ``dt = max(0, worked - dt_daily)``, ``ot = max(0, min(worked, dt_daily) - ot_daily)``,
   ``regular = worked - ot - dt`` (absent thresholds -> no split).
2. Per week (``week_start``): cumulative regular hours beyond ``ot_weekly`` move from regular to OT on the day
   they occur; ``prior_regular`` seeds a week that began in the previous period (read-only lookup).

Invariant: ``regular + ot + double_time == worked`` for every day.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal("0.00")


@dataclasses.dataclass(frozen=True, slots=True)
class DayInput:
    date: date
    worked: Decimal
    fixed: Mapping[str, Decimal] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class OtRules:
    ot_daily: Decimal | None = None
    dt_daily: Decimal | None = None
    ot_weekly: Decimal | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class DaySplit:
    date: date
    worked: Decimal
    regular: Decimal
    ot: Decimal
    double_time: Decimal


def week_of(d: date, week_start: int) -> date:
    return d - timedelta(days=(d.weekday() - week_start) % 7)


def split(
    days: Sequence[DayInput],
    rules: OtRules,
    week_start: int = 0,
    prior_regular: Mapping[date, Decimal] | None = None,
) -> list[DaySplit]:
    out: list[DaySplit] = []
    cumulative: dict[date, Decimal] = dict(prior_regular or {})
    for day in sorted(days, key=lambda d: d.date):
        worked = day.worked
        if worked < 0:
            msg = "worked hours must not be negative"
            raise ValueError(msg)
        dt = ZERO
        ot = ZERO
        if rules.dt_daily is not None:
            dt = max(ZERO, worked - rules.dt_daily)
        if rules.ot_daily is not None:
            cap = min(worked, rules.dt_daily) if rules.dt_daily is not None else worked
            ot = max(ZERO, cap - rules.ot_daily)
        regular = worked - ot - dt
        if rules.ot_weekly is not None:
            key = week_of(day.date, week_start)
            so_far = cumulative.get(key, ZERO)
            room = max(ZERO, rules.ot_weekly - so_far)
            moved = max(ZERO, regular - room)
            regular -= moved
            ot += moved
            cumulative[key] = so_far + regular
        out.append(DaySplit(day.date, worked, regular, ot, dt))
    return out
