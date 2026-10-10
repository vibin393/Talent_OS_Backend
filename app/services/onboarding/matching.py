# ruff: noqa: E501
"""Template matching (Epic 23 A3, FR-122) -- pure.

A template matches when each of its non-NULL match columns equals the Placement fact. The most specific wins
(count of non-NULL columns), ties broken by the column order Client > Legal Entity > Worker Type > country, then
the newest (``created_at``, then ``id`` for a total order). The result never depends on the input order.
"""

import dataclasses
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

MATCH_COLUMNS = ("client_id", "legal_entity_id", "worker_type", "country")


@dataclasses.dataclass(frozen=True)
class PlacementFacts:
    client_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    worker_type: str | None
    country: str | None


class Matchable(Protocol):
    id: uuid.UUID
    client_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    worker_type: str | None
    country: str | None
    created_at: datetime


def matches(template: Matchable, facts: PlacementFacts) -> bool:
    for col in MATCH_COLUMNS:
        want = getattr(template, col)
        if want is None:
            continue
        have = getattr(facts, col)
        if isinstance(want, str) and isinstance(have, str):
            if want.lower() != have.lower():
                return False
        elif want != have:
            return False
    return True


def _rank(template: Matchable) -> tuple[int, tuple[int, ...], datetime, str]:
    present = tuple(int(getattr(template, col) is not None) for col in MATCH_COLUMNS)
    return (sum(present), present, template.created_at, str(template.id))


def select[T: Matchable](templates: Iterable[T], facts: PlacementFacts) -> T | None:
    candidates = [t for t in templates if matches(t, facts)]
    if not candidates:
        return None
    return max(candidates, key=_rank)
