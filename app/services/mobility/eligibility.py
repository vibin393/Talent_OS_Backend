# ruff: file-ignore[line-too-long]
"""Eligibility of an Employee for an internal opening (Epic 41 A2). ``evaluate`` is pure; ``load_rules`` reads the
per-Demand rules. Reasons are user-facing strings.

* tenure = today - current Placement ``actual_start_date`` (Worker) or - ``users.created_at`` (User) >= the rule's
  ``min_tenure_months`` (or the tenant default);
* grade in ``grades`` (a missing grade is not eligible when the rule requires one);
* location in ``locations`` (Worker: the Placement's work location; User: the mobility profile location if set,
  else skipped); a rule location matches when one contains the other, case-insensitively;
* ``business_unit_ids`` (Users only; Workers have no business unit).
"""

import dataclasses
import uuid
from collections.abc import Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mobility import InternalOpeningRule
from app.services.mobility.employees import Employee


@dataclasses.dataclass(frozen=True, slots=True)
class Rules:
    min_tenure_months: int | None = None
    grades: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    business_unit_ids: tuple[uuid.UUID, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...]


def months_between(start: date | None, end: date) -> int:
    if start is None or start > end:
        return 0
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _loc_match(rule_loc: str, emp_loc: str) -> bool:
    a, b = rule_loc.strip().lower(), emp_loc.strip().lower()
    return bool(a and b) and (a in b or b in a)


def evaluate(rules: Rules | None, default_min_tenure: int | None, emp: Employee, today: date) -> EligibilityResult:
    rules = rules or Rules()
    reasons: list[str] = []
    min_tenure = rules.min_tenure_months if rules.min_tenure_months is not None else default_min_tenure
    if min_tenure:
        have = months_between(emp.tenure_start, today)
        if have < min_tenure:
            reasons.append(
                f"Requires at least {_plural(min_tenure, 'month')} in your current role (you have {_plural(have, 'month')})."
            )
    if rules.grades:
        wanted = {g.strip().lower() for g in rules.grades}
        listed = ", ".join(rules.grades)
        if not emp.grade:
            reasons.append(f"Open to grades {listed}; no grade is recorded for you.")
        elif emp.grade.strip().lower() not in wanted:
            reasons.append(f"Open to grades {listed} (your grade: {emp.grade}).")
    if rules.locations:
        listed = ", ".join(rules.locations)
        if emp.location:
            if not any(_loc_match(loc, emp.location) for loc in rules.locations):
                reasons.append(f"Open to employees based in {listed}.")
        elif emp.kind == "worker":
            reasons.append(f"Open to employees based in {listed}; your work location is not recorded.")
    if rules.business_unit_ids and emp.kind == "user" and emp.business_unit_id not in rules.business_unit_ids:
        reasons.append("Open to other business units only.")
    return EligibilityResult(not reasons, tuple(reasons))


def rules_of(row: InternalOpeningRule | None) -> Rules | None:
    if row is None:
        return None
    return Rules(
        min_tenure_months=row.min_tenure_months,
        grades=tuple(row.grades or ()),
        locations=tuple(row.locations or ()),
        business_unit_ids=tuple(row.business_unit_ids or ()),
    )


async def load_rules(db: AsyncSession, demand_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Rules]:
    ids = list(set(demand_ids))
    if not ids:
        return {}
    rows = (await db.execute(select(InternalOpeningRule).where(InternalOpeningRule.demand_id.in_(ids)))).scalars()
    return {r.demand_id: rules_of(r) for r in rows}  # type: ignore[misc]
