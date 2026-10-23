# ruff: file-ignore[line-too-long]
"""Deterministic chase cadence of the Compliance Agent (Epic 62 A3, FR-308; pure, hypothesis-tested).

``due_chases(facts, today, cfg)`` returns the items to remind today and whether to escalate:

* Items: mandatory checklist items ``missing`` / ``expiring`` / ``expired`` of open Placements, measured against the
  Placement start date while today is before it (else the linked credential expiry); credentials and permits with an
  ``expires_on``.
* An item is due when ``due_on - today`` is one of ``cfg.offsets`` (14 / 7 / 3 / 1). After the deadline it is due every
  ``post_deadline_every_days`` (3) days, at most ``max_post_deadline_chases`` (3) times.
* Skips (no duplicate chasing on top of P2): the item already chased at this offset; the person contacted by P2 or the
  agent in the last ``person_quiet_hours`` (20 h); the item contacted in the last ``item_quiet_hours`` (48 h); an expiry
  day that is one of the credential type's P2 ``alert_offsets`` (P2 notifies on those days).
* ``escalate`` = any chaseable item with ``days_left <= escalate_within_days`` (2), or the start date within
  ``portal_window_days`` (7) and the portal never opened.

Cadence values are code constants in Phase 4 (shown read-only in the UI; tenant overrides are a known gap).
"""

import dataclasses
import uuid
from datetime import date, datetime, timedelta
from typing import Literal

CHECKLIST_CHASE_STATUSES = ("missing", "expiring", "expired")
ItemKind = Literal["checklist_item", "credential", "permit"]


@dataclasses.dataclass(frozen=True, slots=True)
class PlanConfig:
    offsets: tuple[int, ...] = (14, 7, 3, 1)
    post_deadline_every_days: int = 3
    max_post_deadline_chases: int = 3
    escalate_within_days: int = 2
    portal_window_days: int = 7
    person_quiet_hours: int = 20
    item_quiet_hours: int = 48


DEFAULT_CONFIG = PlanConfig()


@dataclasses.dataclass(frozen=True, slots=True)
class ItemFact:
    item_kind: ItemKind
    item_ref: uuid.UUID
    label: str
    status: str
    placement_id: uuid.UUID | None = None
    start_date: date | None = None
    expires_on: date | None = None
    mandatory: bool = True
    p2_alert_offsets: tuple[int, ...] = ()
    last_item_contact: datetime | None = None
    chased_offsets: frozenset[int] = frozenset()
    post_deadline_chases: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class ChaseFacts:
    candidate_id: uuid.UUID
    now: datetime
    items: tuple[ItemFact, ...] = ()
    last_contact: datetime | None = None
    portal_last_seen: datetime | None = None
    start_dates: tuple[date, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class DueItem:
    item_kind: ItemKind
    item_ref: uuid.UUID
    label: str
    due_basis: Literal["start_date", "expiry"]
    due_on: date
    days_left: int
    offset_days: int
    placement_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ChasePlan:
    items: tuple[DueItem, ...]
    escalate: bool
    reasons: tuple[str, ...]
    skipped: tuple[tuple[uuid.UUID, str], ...] = ()

    def offsets_by_ref(self) -> dict[uuid.UUID, int]:
        return {i.item_ref: i.offset_days for i in self.items}


def chaseable(item: ItemFact) -> bool:
    if item.item_kind == "checklist_item":
        return item.mandatory and item.status in CHECKLIST_CHASE_STATUSES
    return item.expires_on is not None and item.status != "renewed"


def basis(item: ItemFact, today: date) -> tuple[Literal["start_date", "expiry"], date] | None:
    if item.item_kind == "checklist_item" and item.start_date is not None:
        if today < item.start_date or item.expires_on is None:
            return "start_date", item.start_date
    if item.expires_on is not None:
        return "expiry", item.expires_on
    return None


def offset_for(days_left: int, item: ItemFact, cfg: PlanConfig) -> int | None:
    if days_left in cfg.offsets:
        return days_left
    if days_left < 0:
        overdue = -days_left
        if (
            overdue % cfg.post_deadline_every_days == 0
            and item.post_deadline_chases < cfg.max_post_deadline_chases
        ):
            return days_left
    return None


def due_chases(facts: ChaseFacts, today: date, cfg: PlanConfig = DEFAULT_CONFIG) -> ChasePlan:
    due: list[DueItem] = []
    skipped: list[tuple[uuid.UUID, str]] = []
    reasons: list[str] = []
    person_recent = facts.last_contact is not None and facts.now - facts.last_contact < timedelta(
        hours=cfg.person_quiet_hours
    )
    deadline_close = False
    for item in facts.items:
        if not chaseable(item):
            continue
        b = basis(item, today)
        if b is None:
            continue
        due_basis, due_on = b
        days_left = (due_on - today).days
        if days_left <= cfg.escalate_within_days:
            deadline_close = True
        offset = offset_for(days_left, item, cfg)
        if offset is None:
            continue
        if offset in item.chased_offsets:
            skipped.append((item.item_ref, "already_chased"))
            continue
        if person_recent:
            skipped.append((item.item_ref, "recently_contacted"))
            continue
        if item.last_item_contact is not None and facts.now - item.last_item_contact < timedelta(
            hours=cfg.item_quiet_hours
        ):
            skipped.append((item.item_ref, "item_recently_contacted"))
            continue
        if due_basis == "expiry" and days_left in item.p2_alert_offsets:
            skipped.append((item.item_ref, "p2_alert_day"))
            continue
        due.append(
            DueItem(
                item.item_kind,
                item.item_ref,
                item.label,
                due_basis,
                due_on,
                days_left,
                offset,
                item.placement_id,
            )
        )
    if deadline_close:
        reasons.append("deadline_close")
    soon = [d for d in facts.start_dates if 0 <= (d - today).days <= cfg.portal_window_days]
    if soon and facts.portal_last_seen is None and any(chaseable(i) for i in facts.items):
        reasons.append("portal_not_opened")
    return ChasePlan(tuple(due), bool(reasons), tuple(reasons), tuple(skipped))


__all__ = [
    "CHECKLIST_CHASE_STATUSES",
    "DEFAULT_CONFIG",
    "ChaseFacts",
    "ChasePlan",
    "DueItem",
    "ItemFact",
    "PlanConfig",
    "basis",
    "chaseable",
    "due_chases",
    "offset_for",
]
