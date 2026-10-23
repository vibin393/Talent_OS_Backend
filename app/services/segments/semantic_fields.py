# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Fields Epic 38 adds to the base semantic sources (decision 11, spec A3), via ``extend_semantic_source``.

* ``candidates.candidate.days_since_last_placement`` (int; ``gt`` / ``lt`` / ``is_null``; permission
  ``placement:read``): days since the Candidate's latest ``placements.created_at``; NULL when never placed.
* ``placements.worker.offboarding.reason`` (enum of the P2 offboarding case reasons; ``eq`` / ``in`` /
  ``not_in``): the reason of the Placement's latest ``offboarding_cases`` row (one-join).

Both are correlated expressions built from registered models; user input never reaches SQL text.
"""

from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import aliased

from app.core.semantic import FieldDef, JoinDef, extend_semantic_source, get_source
from app.models.placements import Placement
from app.models.placements_exit import CASE_REASONS, OffboardingCase

__all__ = ["CANDIDATE_FIELDS", "WORKER_FIELDS", "register"]

CANDIDATE_FIELDS = ("days_since_last_placement",)
WORKER_FIELDS = ("offboarding.reason",)

_queued: set[str] = set()


def _days_since_last_placement(c: Any) -> Any:
    p = aliased(Placement)
    latest = select(func.max(p.created_at)).where(p.candidate_id == c.id).scalar_subquery()
    return func.current_date() - cast(latest, Date)


def _latest_case() -> Any:
    o = aliased(OffboardingCase)
    return (
        select(o.placement_id.label("placement_id"), o.reason.label("reason"))
        .distinct(o.placement_id)
        .order_by(o.placement_id, o.created_at.desc())
        .subquery("latest_offboarding")
    )


def _missing(source: str, key: str) -> bool:
    """True when ``source`` is registered without ``key``; an unregistered source is extended (queued) once."""
    from app.core.semantic import sources

    if source in sources():
        return key not in get_source(source).fields
    if source in _queued:
        return False
    _queued.add(source)
    return True


def register() -> None:
    """Idempotent; safe to call again (re-applies the fields if a test re-registered a base source)."""
    if _missing("candidates.candidate", "days_since_last_placement"):
        extend_semantic_source(
            "candidates.candidate",
            fields={
                "days_since_last_placement": FieldDef(
                    "int",
                    "Days since last placement",
                    "none",
                    _days_since_last_placement,
                    frozenset({"gt", "lt", "is_null"}),
                    permission="placement:read",
                )
            },
        )
    if _missing("placements.worker", "offboarding.reason"):
        extend_semantic_source(
            "placements.worker",
            fields={
                "offboarding.reason": FieldDef(
                    "enum",
                    "Offboarding reason",
                    "none",
                    lambda o: o.c.reason,
                    frozenset({"eq", "in", "not_in"}),
                    enum_values=CASE_REASONS,
                    join="offboarding",
                )
            },
            joins={"offboarding": JoinDef(_latest_case(), lambda p, o: o.c.placement_id == p.id)},
        )
