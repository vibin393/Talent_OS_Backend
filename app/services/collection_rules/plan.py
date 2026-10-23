# ruff: file-ignore[line-too-long]
"""Collection plan (Epic 63 A4, FR-314). Pure: ``collection_plan(facts) -> CollectionPlan(items, excluded)``.

Per document (issued invoice / debit note with a due date): ``days_overdue = today - due_date`` (negative before the
due date). The due step is the latest active step whose ``offset_days`` was reached; it is due unless it already ran
for the document (agent log or P2 activity). Exclusions, in order: ``not_open``, ``do_not_remind``, ``held`` (Client or
document paused), ``takeover``, ``disputed`` (fully), ``no_step_due``, ``recently_contacted`` (a P2 email activity in
the last 3 days). Documents due for the same step are grouped into one message per Client; then the Client-level
``quiet_hours`` (rule time zone) and ``weekly_cap`` (messages in the last 7 days) apply to the grouped items.

Only the undisputed balance is remindable. ``send_class`` is ``pre_due_auto_eligible`` only for a templated pre-due
step with ``allow_auto`` to billing contacts and no partially disputed document; everything else ``needs_approval``.
Disputed or held documents are never in ``items`` (hypothesis-tested).
"""

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from datetime import time as time
from decimal import Decimal

ZERO = Decimal("0.00")


@dataclasses.dataclass(frozen=True, slots=True)
class DocFacts:
    kind: str
    id: uuid.UUID
    number: str | None
    due_date: date | None
    currency: str
    open_amount: Decimal
    disputed_amount: Decimal = ZERO
    status: str = "issued"
    written_off: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class StepFacts:
    id: uuid.UUID
    position: int
    offset_days: int
    tone: str
    audience: str = "billing_contacts"
    email_template_id: uuid.UUID | None = None
    allow_auto: bool = False
    create_task: bool = False
    active: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class PlanFacts:
    client_id: uuid.UUID
    today: date
    docs: tuple[DocFacts, ...]
    steps: tuple[StepFacts, ...]
    now_local: datetime | None = None
    rule_active: bool = True
    do_not_remind: bool = False
    client_hold: str | None = None  # "paused" | None
    doc_holds: Mapping[uuid.UUID, str] = dataclasses.field(default_factory=dict)  # doc -> paused | takeover
    done: frozenset[tuple[uuid.UUID, uuid.UUID]] = frozenset()  # (document, step) already sent / proposed
    recently_contacted: frozenset[uuid.UUID] = frozenset()
    sent_last_7_days: int = 0
    max_per_week: int = 2
    quiet_start: time = time(18, 0)
    quiet_end: time = time(9, 0)


@dataclasses.dataclass(frozen=True, slots=True)
class PlanDoc:
    doc: DocFacts
    days_overdue: int
    remindable: Decimal


@dataclasses.dataclass(frozen=True, slots=True)
class PlanItem:
    step: StepFacts
    docs: tuple[PlanDoc, ...]
    send_class: str
    currency: str
    total_remindable: Decimal
    partially_disputed: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Exclusion:
    document_id: uuid.UUID
    number: str | None
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class CollectionPlan:
    items: tuple[PlanItem, ...]
    excluded: tuple[Exclusion, ...]


def in_quiet_hours(now: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def due_step(steps: tuple[StepFacts, ...], days_overdue: int) -> StepFacts | None:
    reached = [s for s in steps if s.active and s.offset_days <= days_overdue]
    return max(reached, key=lambda s: (s.offset_days, s.position)) if reached else None


def _send_class(step: StepFacts, docs: tuple[PlanDoc, ...], partial: bool) -> str:
    eligible = (
        step.allow_auto
        and step.offset_days < 0
        and step.email_template_id is not None
        and step.audience == "billing_contacts"
        and step.tone in ("courteous", "friendly")
        and not partial
        and all(d.days_overdue < 0 for d in docs)
    )
    return "pre_due_auto_eligible" if eligible else "needs_approval"


def collection_plan(facts: PlanFacts) -> CollectionPlan:
    excluded: list[Exclusion] = []
    grouped: dict[tuple[uuid.UUID, str], list[PlanDoc]] = {}
    steps_by_id = {s.id: s for s in facts.steps}
    for doc in facts.docs:
        def skip(reason: str, d: DocFacts = doc) -> None:
            excluded.append(Exclusion(d.id, d.number, reason))

        if doc.status != "issued" or doc.written_off or doc.open_amount <= 0 or doc.due_date is None:
            skip("not_open")
            continue
        if facts.do_not_remind or not facts.rule_active:
            skip("do_not_remind")
            continue
        hold = facts.doc_holds.get(doc.id)
        if facts.client_hold is not None or hold == "paused":
            skip("held")
            continue
        if hold == "takeover":
            skip("takeover")
            continue
        remindable = (doc.open_amount - max(doc.disputed_amount, ZERO)).quantize(Decimal("0.01"))
        if remindable <= 0:
            skip("disputed")
            continue
        days = (facts.today - doc.due_date).days
        step = due_step(facts.steps, days)
        if step is None or (doc.id, step.id) in facts.done:
            skip("no_step_due")
            continue
        if doc.id in facts.recently_contacted:
            skip("recently_contacted")
            continue
        grouped.setdefault((step.id, doc.currency), []).append(PlanDoc(doc, days, remindable))
    items: list[PlanItem] = []
    for (step_id, currency), docs in sorted(grouped.items(), key=lambda kv: (steps_by_id[kv[0][0]].position, kv[0][1])):
        step = steps_by_id[step_id]
        partial = any(d.doc.disputed_amount > 0 for d in docs)
        t = tuple(sorted(docs, key=lambda d: (d.doc.due_date or date.max, d.doc.number or "")))
        items.append(
            PlanItem(
                step=step, docs=t, send_class=_send_class(step, t, partial), currency=currency,
                total_remindable=sum((d.remindable for d in t), ZERO), partially_disputed=partial,
            )
        )  # fmt: skip
    if items and facts.now_local is not None and in_quiet_hours(facts.now_local.time(), facts.quiet_start, facts.quiet_end):
        excluded += [Exclusion(d.doc.id, d.doc.number, "quiet_hours") for i in items for d in i.docs]
        items = []
    remaining = max(facts.max_per_week - facts.sent_last_7_days, 0)
    if len(items) > remaining:
        for item in items[remaining:]:
            excluded += [Exclusion(d.doc.id, d.doc.number, "weekly_cap") for d in item.docs]
        items = items[:remaining]
    return CollectionPlan(items=tuple(items), excluded=tuple(excluded))


__all__ = [
    "CollectionPlan",
    "DocFacts",
    "Exclusion",
    "PlanDoc",
    "PlanFacts",
    "PlanItem",
    "StepFacts",
    "collection_plan",
    "due_step",
    "in_quiet_hours",
]
