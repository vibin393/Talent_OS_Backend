# ruff: noqa: E501, D401
"""Program eligibility, reward amount and exclusions (Epic 42 story 42.1, FR-213).

The functions at the top are pure (``ProgramFacts`` / ``DemandFacts`` / ``Referrer`` in, decisions out); the
loaders at the bottom build the facts from the database.

* ``program_for`` -- the highest-priority active program in its validity window whose ``eligible_referrers`` and
  ``eligible_demands`` match. ``None`` = no program (the referral is still accepted, ``reward_eligible=false``,
  ``ineligible_reason='no_program'``). General referrals (no Demand) never get a program.
* ``reward_amount`` -- the first matching reward rule, else ``default_amount`` (Decimal, AD-20).
* ``check_exclusions`` -- at referral time: an assigned recruiter, the Demand owner or a holder of an excluded
  Role is refused (409 ``referrer_not_eligible``).
* ``check_self_referral`` -- the referee is the referrer (same email, or the Worker themself) -> 422.

[ASSUMPTION] Demands carry no ``seniority_level`` column; seniority is derived from ``min_experience_years``:
``< 3`` junior, ``< 7`` mid, otherwise senior (no minimum -> unknown, never matches a seniority rule).
"""

import dataclasses
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.referrals.common import Referrer, conflict, invalid

JUNIOR_BELOW = Decimal(3)
SENIOR_FROM = Decimal(7)


@dataclasses.dataclass(frozen=True, slots=True)
class DemandFacts:
    id: uuid.UUID
    client_id: uuid.UUID | None
    employment_type: str | None
    min_experience_years: Decimal | None
    owner_id: uuid.UUID | None
    assignee_user_ids: frozenset[uuid.UUID] = frozenset()

    @property
    def seniority_level(self) -> str | None:
        return seniority_of(self.min_experience_years)


@dataclasses.dataclass(frozen=True, slots=True)
class ProgramFacts:
    id: uuid.UUID
    active: bool
    valid_from: date
    valid_to: date | None
    eligible_referrers: str
    eligible_demands: Mapping[str, Any]
    reward_rules: Sequence[Mapping[str, Any]]
    default_amount: Decimal | None
    currency: str
    joining_condition_days: int
    exclusions: Mapping[str, Any]
    priority: int
    archived: bool = False

    @classmethod
    def of(cls, row: Any) -> "ProgramFacts":
        return cls(
            id=row.id,
            active=bool(row.active),
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            eligible_referrers=row.eligible_referrers,
            eligible_demands=dict(row.eligible_demands or {}),
            reward_rules=list(row.reward_rules or []),
            default_amount=Decimal(row.default_amount) if row.default_amount is not None else None,
            currency=row.currency,
            joining_condition_days=int(row.joining_condition_days),
            exclusions=dict(row.exclusions or {}),
            priority=int(row.priority),
            archived=row.archived_at is not None,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Reward:
    amount: Decimal
    currency: str


def seniority_of(min_years: Decimal | None) -> str | None:
    if min_years is None:
        return None
    years = Decimal(min_years)
    if years < JUNIOR_BELOW:
        return "junior"
    if years < SENIOR_FROM:
        return "mid"
    return "senior"


def _ids(values: Iterable[Any]) -> set[str]:
    return {str(v) for v in values or ()}


def referrer_matches(program: ProgramFacts, kind: str) -> bool:
    return program.eligible_referrers == "both" or program.eligible_referrers == f"{kind}s"


def demand_matches(program: ProgramFacts, demand: DemandFacts) -> bool:
    spec = program.eligible_demands or {}
    if spec.get("all"):
        return True
    checks: list[bool] = []
    if spec.get("demand_ids"):
        checks.append(str(demand.id) in _ids(spec["demand_ids"]))
    if spec.get("client_ids"):
        checks.append(str(demand.client_id) in _ids(spec["client_ids"]))
    if spec.get("employment_types"):
        checks.append(demand.employment_type in set(spec["employment_types"]))
    if spec.get("seniority_levels"):
        checks.append(demand.seniority_level in set(spec["seniority_levels"]))
    return bool(checks) and all(checks)


def in_window(program: ProgramFacts, on: date) -> bool:
    return program.valid_from <= on and (program.valid_to is None or on <= program.valid_to)


def program_for(
    programs: Iterable[ProgramFacts], demand: DemandFacts | None, referrer_kind: str, on: date
) -> ProgramFacts | None:
    """The highest-priority matching program (ties: earliest ``valid_from``, then id) or None."""
    if demand is None:
        return None
    candidates = [
        p
        for p in programs
        if p.active
        and not p.archived
        and in_window(p, on)
        and referrer_matches(p, referrer_kind)
        and demand_matches(p, demand)
    ]
    candidates.sort(key=lambda p: (-p.priority, p.valid_from, str(p.id)))
    return candidates[0] if candidates else None


def _rule_matches(match: Mapping[str, Any], demand: DemandFacts) -> bool:
    keys = {k: v for k, v in (match or {}).items() if v is not None}
    if not keys:
        return False
    if "seniority_level" in keys and keys["seniority_level"] != demand.seniority_level:
        return False
    if "demand_id" in keys and str(keys["demand_id"]) != str(demand.id):
        return False
    return not ("employment_type" in keys and keys["employment_type"] != demand.employment_type)


def reward_amount(program: ProgramFacts, demand: DemandFacts) -> Reward | None:
    """The first matching rule's amount, else ``default_amount``; None when neither applies."""
    for rule in program.reward_rules:
        if _rule_matches(rule.get("match") or {}, demand):
            return Reward(Decimal(str(rule["amount"])), str(rule.get("currency") or program.currency).upper())
    if program.default_amount is not None:
        return Reward(Decimal(program.default_amount), program.currency)
    return None


def exclusion_reason(program: ProgramFacts, demand: DemandFacts, referrer: Referrer) -> str | None:
    """``assigned_recruiter`` | ``demand_owner`` | ``excluded_role`` | None. Only Users can be excluded."""
    if referrer.kind != "user":
        return None
    ex = program.exclusions or {}
    if ex.get("assigned_recruiters", True) and referrer.id in demand.assignee_user_ids:
        return "assigned_recruiter"
    if ex.get("demand_owner", True) and demand.owner_id == referrer.id:
        return "demand_owner"
    roles = set(ex.get("excluded_role_keys") or ())
    if roles & set(referrer.role_keys):
        return "excluded_role"
    return None


def check_exclusions(program: ProgramFacts | None, demand: DemandFacts | None, referrer: Referrer) -> None:
    """409 ``referrer_not_eligible``. Without a program the assignment / ownership rule still applies (a recruiter
    working the Demand adds the candidate through sourcing, never as a referral)."""
    if demand is None:
        return
    effective = program or ProgramFacts(
        id=uuid.UUID(int=0),
        active=True,
        valid_from=date.min,
        valid_to=None,
        eligible_referrers="both",
        eligible_demands={"all": True},
        reward_rules=(),
        default_amount=None,
        currency="INR",
        joining_condition_days=0,
        exclusions={"assigned_recruiters": True, "demand_owner": True},
        priority=0,
    )
    reason = exclusion_reason(effective, demand, referrer)
    if reason is not None:
        raise conflict(
            "referrer_not_eligible",
            "You work on this opening. Add the candidate through sourcing instead",
            rule=reason,
        )


def check_self_referral(
    referrer: Referrer, referee_email: str | None, referee_candidate_id: uuid.UUID | None = None
) -> None:
    same_email = bool(
        referee_email
        and referrer.email
        and referee_email.strip().casefold() == referrer.email.strip().casefold()
    )
    same_worker = (
        referrer.kind == "worker" and referee_candidate_id is not None and referee_candidate_id == referrer.id
    )
    if same_email or same_worker:
        raise invalid("self_referral", "You cannot refer yourself")


# ================================================================ loaders
async def load_programs(db: AsyncSession) -> list[ProgramFacts]:
    from app.models.referrals import ReferralProgram

    rows = (
        await db.execute(
            select(ReferralProgram).where(
                ReferralProgram.active.is_(True), ReferralProgram.archived_at.is_(None)
            )
        )
    ).scalars()
    return [ProgramFacts.of(r) for r in rows]


async def load_program(db: AsyncSession, program_id: uuid.UUID | None) -> ProgramFacts | None:
    from app.models.referrals import ReferralProgram

    if program_id is None:
        return None
    row = await db.get(ReferralProgram, program_id)
    return ProgramFacts.of(row) if row is not None else None


async def demand_facts(db: AsyncSession, demand_id: uuid.UUID | None) -> DemandFacts | None:
    from app.models.demands import Demand, DemandAssignment

    if demand_id is None:
        return None
    d = await db.get(Demand, demand_id)
    if d is None:
        return None
    assignees = frozenset(
        (
            await db.execute(
                select(DemandAssignment.user_id).where(
                    DemandAssignment.demand_id == d.id,
                    DemandAssignment.removed_at.is_(None),
                    DemandAssignment.user_id.is_not(None),
                )
            )
        ).scalars()
    )
    return DemandFacts(
        d.id,
        d.client_id,
        d.employment_type,
        d.min_experience_years,
        d.owner_id,
        frozenset(a for a in assignees if a),
    )
