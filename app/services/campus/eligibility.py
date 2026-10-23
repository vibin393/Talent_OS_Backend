# ruff: file-ignore[line-too-long]
"""Eligibility rules and overrides (Epic 43 story 43.3, FR-218, AD-42).

The drive's rule is an AD-42 definition over ``campus.registration`` (validated on save with the editor's
principal; 422 codes from the foundation). ``evaluate`` asks ``core.semantic.ids`` (system context, purpose
``eligibility``) for ``and[drive_id eq, id in <batch <= 500>, <rule>]``; for the rest, each top-level clause is
evaluated alone the same way and the **first failing clause** is recorded (``failing_rule = {index, field, op,
value, label}``). No raw SQL. Runs after each registration (task ``campus.evaluate_eligibility``) and when the rule
changes (every ``registered|eligible|not_eligible`` registration without an override).

The override (star, FR-257) is human only: ``core.actors.require_human`` (403 ``human_only_action``) plus the
``campus_registration: eligibility_override`` entry of the workflow catalogue; it survives re-evaluation.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core import semantic as sem
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.core.permissions import Principal
from app.core.semantic import AndNode, Condition, Definition, OrNode
from app.models.campus import CampusDrive, Registration
from app.schemas.campus import DriveOut, RegistrationOut
from app.services.campus.common import SEMANTIC_SOURCE, conflict, drive_for, now, principal_of
from app.services.campus.domain_events import EligibilityDefinitionChanged

BATCH = 500
EVALUABLE = ("registered", "eligible", "not_eligible")
SYMBOLS = {
    "eq": "=",
    "ne": "≠",
    "in": "in",
    "not_in": "not in",
    "lt": "<",
    "lte": "≤",
    "gt": ">",
    "gte": "≥",
    "between": "between",
    "is_null": "is empty",
    "within_days": "within days",
    "contains": "contains",
    "starts_with": "starts with",
}


# ================================================================ definitions
def _definition(stored: dict[str, Any] | None) -> Definition | None:
    if not stored:
        return None
    return Definition.model_validate(stored)


def clauses(defn: Definition | None) -> list[Any]:
    if defn is None or defn.filters is None:
        return []
    if isinstance(defn.filters, AndNode):
        return list(defn.filters.and_)
    return [defn.filters]


def _node_json(node: Any) -> dict[str, Any]:
    return node.model_dump(by_alias=True, mode="json")  # type: ignore[no-any-return]


def wrapped(drive_id: uuid.UUID, ids: list[uuid.UUID], node: Any | None) -> Definition:
    items: list[dict[str, Any]] = [
        {"field": "drive_id", "op": "eq", "value": str(drive_id)},
        {"field": "id", "op": "in", "value": [str(i) for i in ids]},
    ]
    if node is not None:
        items.append(node if isinstance(node, dict) else _node_json(node))
    return Definition.model_validate({"source": SEMANTIC_SOURCE, "filters": {"and": items}})


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return str(Decimal(str(value)))
    return str(value)


def clause_label(node: Any, index: int) -> tuple[str, str | None, str | None, Any]:
    """``(label, field, op, value)`` of a top-level clause, e.g. ``("CGPA ≥ 7.0", "cgpa", "gte", 7.0)``."""
    if isinstance(node, Condition):
        try:
            fd = sem.get_source(SEMANTIC_SOURCE).fields.get(node.field)
        except KeyError:  # pragma: no cover - registered at start-up
            fd = None
        name = fd.label if fd is not None else node.field
        sym = SYMBOLS.get(node.op, node.op)
        if node.op == "between" and isinstance(node.value, list) and len(node.value) == 2:
            text = f"{name} between {_fmt(node.value[0])} and {_fmt(node.value[1])}"
        elif node.op == "is_null":
            text = f"{name} {'is empty' if node.value else 'is not empty'}"
        else:
            text = f"{name} {sym} {_fmt(node.value)}"
        return text, node.field, node.op, node.value
    kind = "any of" if isinstance(node, OrNode) else "all of"
    inner = node.or_ if isinstance(node, OrNode) else node.and_
    parts = [clause_label(n, index)[0] for n in inner[:3]]
    more = "" if len(inner) <= 3 else ", ..."
    return f"Rule {index + 1}: {kind} ({'; '.join(parts)}{more})", None, None, None


def failing_rule(defn: Definition | None, index: int) -> dict[str, Any]:
    items = clauses(defn)
    node = items[index] if 0 <= index < len(items) else None
    if node is None:
        return {"index": index, "field": None, "op": None, "value": None, "label": "Does not meet the eligibility rule"}
    label, field, op, value = clause_label(node, index)
    return {"index": index, "field": field, "op": op, "value": value, "label": label}


def validate_filters(filters: dict[str, Any] | None, principal: Principal | None) -> dict[str, Any] | None:
    """Validate an editor's filter tree (with their field permissions); the composed evaluation query must also
    fit the semantic limits. Returns the stored definition (``None`` = everybody is eligible)."""
    if filters is None:
        return None
    defn = sem.validate({"source": SEMANTIC_SOURCE, "filters": filters}, principal=principal)
    sem.validate(wrapped(uuid.uuid4(), [uuid.uuid4()], defn.filters), principal=principal)
    return defn.model_dump(by_alias=True, mode="json")


async def set_definition(c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, filters: dict[str, Any] | None) -> DriveOut:
    from app.services.campus import drives

    drive = await drive_for(c, db, drive_id, write=True, lock=True)
    if drive.status not in drives.EDITABLE:
        raise conflict("drive_closed", "This drive is closed", status=drive.status)
    drive.eligibility_definition = validate_filters(filters, principal_of(c))
    drive.eligibility_revision += 1
    await db.flush()
    emit(db, EligibilityDefinitionChanged(tenant_id=drive.tenant_id, drive_id=drive.id, revision=drive.eligibility_revision))
    audit.record("campus.eligibility_rule_changed", "campus_drive", drive.id, session=db, revision=drive.eligibility_revision)
    await db.refresh(drive)
    return await drives.drive_out(db, drive)


def field_catalogue(principal: Principal) -> Any:
    return sem.source_catalogue(principal, SEMANTIC_SOURCE)


# ================================================================ evaluation
async def evaluate(db: AsyncSession, drive: CampusDrive, registration_ids: list[uuid.UUID]) -> dict[str, int]:
    """Evaluate up to ``BATCH`` registrations (rows locked with SKIP LOCKED; overrides and later states untouched)."""
    from app.services.campus.registration import set_status

    regs = list(
        (
            await db.execute(
                select(Registration)
                .where(
                    Registration.id.in_(registration_ids[:BATCH]),
                    Registration.drive_id == drive.id,
                    Registration.status.in_(EVALUABLE),
                    Registration.override_at.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    if not regs:
        return {"eligible": 0, "not_eligible": 0}
    defn = _definition(drive.eligibility_definition)
    ids = [r.id for r in regs]
    failing: dict[uuid.UUID, int] = {}
    if defn is not None and defn.filters is not None:
        sctx = sem.SemanticContext.for_system(drive.tenant_id, db, "eligibility")
        ok = set(await sem.ids(wrapped(drive.id, ids, defn.filters), sctx))
        rest = [i for i in ids if i not in ok]
        for index, node in enumerate(clauses(defn)):
            todo = [i for i in rest if i not in failing]
            if not todo:
                break
            passing = set(await sem.ids(wrapped(drive.id, todo, node), sctx))
            for i in todo:
                if i not in passing:
                    failing[i] = index
        for i in rest:
            failing.setdefault(i, 0)
    at = now()
    counts = {"eligible": 0, "not_eligible": 0}
    for r in regs:
        eligible = r.id not in failing
        r.eligibility = {
            "eligible": eligible,
            "failing_rule": None if eligible else failing_rule(defn, failing[r.id]),
            "evaluated_at": at.isoformat(),
            "definition_version": defn.definition_version if defn is not None else None,
            "revision": drive.eligibility_revision,
        }
        target = "eligible" if eligible else "not_eligible"
        if r.status != target:
            set_status(r, target, "system", at=at)
        counts[target] += 1
    await db.flush()
    return counts


async def evaluate_drive(db: AsyncSession, drive_id: uuid.UUID, *, pending_only: bool) -> dict[str, int]:
    """Evaluate every pending (``registered``) -- or, after a rule change, every evaluable -- registration of a drive
    in batches of ``BATCH``."""
    drive = await db.get(CampusDrive, drive_id)
    if drive is None or drive.status in ("closed", "cancelled"):
        return {"eligible": 0, "not_eligible": 0}
    statuses = ("registered",) if pending_only else EVALUABLE
    total = {"eligible": 0, "not_eligible": 0}
    after: uuid.UUID | None = None
    while True:
        stmt = select(Registration.id).where(
            Registration.drive_id == drive.id, Registration.status.in_(statuses), Registration.override_at.is_(None)
        )
        if after is not None:
            stmt = stmt.where(Registration.id > after)
        ids = list((await db.execute(stmt.order_by(Registration.id).limit(BATCH))).scalars())
        if not ids:
            break
        counts = await evaluate(db, drive, ids)
        for k, v in counts.items():
            total[k] += v
        after = ids[-1]
    return total


# ================================================================ override (star)
async def override(c: RequestContext, db: AsyncSession, registration_id: uuid.UUID, reason: str) -> RegistrationOut:
    from app.services.campus.registration import registration_for, registration_outs, set_status

    actor = actor_from_ctx(c)
    require_human(actor)
    text = (reason or "").strip()
    if len(text) < 5:
        from app.services.campus.common import invalid

        raise invalid("reason_required", "A reason of at least 5 characters is required", {"reason": "Required"})
    reg = await registration_for(c, db, registration_id, write=True, lock=True)
    if reg.status != "not_eligible":
        raise conflict("transition_not_allowed", "Only a not-eligible registration can be overridden", status=reg.status)
    set_status(reg, "eligible", "user")
    reg.override_by = actor.id
    reg.override_at = now()
    reg.override_reason = text
    reg.eligibility = {**(reg.eligibility or {}), "eligible": True, "overridden": True}
    await db.flush()
    audit.record("campus.eligibility_overridden", "campus_registration", reg.id, session=db, reason=text)
    return (await registration_outs(db, [reg]))[0]
