r"""Operator value checks and predicates (Epic 36 contract 0.6).

``coerce_value`` turns a JSON value into the typed Python value(s) an operator needs (or raises 422
``semantic_value_type``); ``predicate`` builds the SQL condition with **bound parameters only** -- user values
are never rendered into SQL text. ``contains`` / ``starts_with`` escape ``%``, ``_`` and ``\\`` before ILIKE.

``register_skill_expander`` installs the skills graph expander (Epic 37 registers ``skills_service.expand``);
the default expands each key to itself with weight 1.00.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import ColumnElement, and_, func, not_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.semantic.errors import VALUE_TYPE, SemanticError
from app.core.semantic.types import FieldDef
from app.core.skill_refs import ExpandedSkill, SkillKey

__all__ = [
    "MAX_IN_VALUES",
    "MAX_STRING",
    "SkillExpander",
    "SkillMatch",
    "WithinDays",
    "coerce_value",
    "expand_skills",
    "predicate",
    "register_skill_expander",
]

SkillExpander = Callable[
    [RequestContext, AsyncSession, Sequence[SkillKey], int], Awaitable[list[ExpandedSkill]]
]

MAX_IN_VALUES = 500
MAX_STRING = 1000
MAX_DAYS = 3650


@dataclass(frozen=True, slots=True)
class WithinDays:
    days: int
    direction: str  # "past" | "future"


@dataclass(frozen=True, slots=True)
class SkillMatch:
    ref: SkillKey
    min_proficiency: int | None
    expand: bool


# ---------------------------------------------------------------- skill expander
async def _identity_expander(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,  # noqa: ARG001
    refs: Sequence[SkillKey],
    depth: int,  # noqa: ARG001
) -> list[ExpandedSkill]:
    return [ExpandedSkill(key=r, root=r, weight=Decimal("1.00"), depth=0, via=None) for r in refs]


_expander: SkillExpander = _identity_expander


def register_skill_expander(fn: SkillExpander | None) -> None:
    """Install the skills graph expander (``None`` restores the identity default)."""
    global _expander  # process-wide wiring, set once from hooks
    _expander = fn if fn is not None else _identity_expander


async def expand_skills(ctx: RequestContext, db: AsyncSession, match: SkillMatch) -> list[ExpandedSkill]:
    if not match.expand:
        return await _identity_expander(ctx, db, [match.ref], 0)
    return list(await _expander(ctx, db, [match.ref], 1))


# ---------------------------------------------------------------- value coercion
def _bad(path: str, message: str, **details: Any) -> SemanticError:
    return SemanticError(VALUE_TYPE, message, path, **details)


def _scalar(fd: FieldDef, value: Any, path: str) -> Any:
    t = fd.type
    if value is None:
        raise _bad(path, "A value is required")
    if t in {"string", "string_array"}:
        if not isinstance(value, str) or len(value) > MAX_STRING:
            raise _bad(path, "Expected a text value")
        return value
    if t == "enum":
        if not isinstance(value, str) or value not in (fd.enum_values or ()):
            raise _bad(path, "Expected one of the listed values", allowed=list(fd.enum_values or ()))
        return value
    if t == "bool":
        if not isinstance(value, bool):
            raise _bad(path, "Expected true or false")
        return value
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 2**31 - 1:
            raise _bad(path, "Expected a whole number")
        return value
    if t == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _bad(path, "Expected a number")
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise _bad(path, "Expected a number") from exc
        if not dec.is_finite() or abs(dec) >= Decimal("1e15"):
            raise _bad(path, "Expected a number")
        return dec
    if t == "uuid":
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise _bad(path, "Expected an id") from exc
    if t == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise _bad(path, "Expected a date (YYYY-MM-DD)")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _bad(path, "Expected a date (YYYY-MM-DD)") from exc
    if t == "datetime":
        try:
            dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        except (ValueError, TypeError) as exc:
            raise _bad(path, "Expected a date-time (ISO 8601)") from exc
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    raise _bad(path, f"Values of type {t} are not supported here")


def _within_days(value: Any, path: str) -> WithinDays:
    direction = "past"
    days = value
    if isinstance(value, dict):
        if set(value) - {"days", "direction"} or "days" not in value:
            raise _bad(path, 'Expected {"days": int, "direction": "past"|"future"}')
        days = value["days"]
        direction = value.get("direction", "past")
        if direction not in {"past", "future"}:
            raise _bad(path, "direction must be past or future")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise _bad(path, f"days must be a whole number from 1 to {MAX_DAYS}")
    return WithinDays(days=days, direction=direction)


def _skill_match(value: Any, path: str) -> SkillMatch:
    if not isinstance(value, dict) or "ref" not in value or set(value) - {"ref", "min_proficiency", "expand"}:
        raise _bad(path, 'Expected {"ref": {"kind", "id"}, "min_proficiency", "expand"}')
    ref = value["ref"]
    if not isinstance(ref, dict) or set(ref) != {"kind", "id"} or ref["kind"] not in {"global", "tenant"}:
        raise _bad(f"{path}.ref", 'Expected {"kind": "global"|"tenant", "id": uuid}')
    try:
        key = SkillKey(kind=ref["kind"], id=uuid.UUID(str(ref["id"])))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _bad(f"{path}.ref.id", "Expected a skill id") from exc
    min_p = value.get("min_proficiency")
    if min_p is not None and (isinstance(min_p, bool) or not isinstance(min_p, int) or not 1 <= min_p <= 5):
        raise _bad(f"{path}.min_proficiency", "min_proficiency must be 1 to 5")
    expand = value.get("expand", False)
    if not isinstance(expand, bool):
        raise _bad(f"{path}.expand", "expand must be true or false")
    return SkillMatch(ref=key, min_proficiency=min_p, expand=expand)


def coerce_value(fd: FieldDef, op: str, value: Any, path: str) -> Any:
    """Typed operator value (raises 422 ``semantic_value_type``). ``path`` points at the condition value."""
    if op in {"eq", "ne", "lt", "lte", "gt", "gte", "contains", "starts_with"}:
        if isinstance(value, (list, dict)):
            raise _bad(path, "Expected a single value")
        out = _scalar(fd, value, path)
        if op in {"contains", "starts_with"} and (not isinstance(out, str) or out == ""):
            raise _bad(path, "Expected non-empty text")
        return out
    if op in {"in", "not_in"}:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
            raise _bad(path, f"Expected a list of 1 to {MAX_IN_VALUES} values")
        return [_scalar(fd, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise _bad(path, "Expected [low, high]")
        return (_scalar(fd, value[0], f"{path}[0]"), _scalar(fd, value[1], f"{path}[1]"))
    if op == "is_null":
        if not isinstance(value, bool):
            raise _bad(path, "Expected true or false")
        return value
    if op == "within_days":
        return _within_days(value, path)
    if op == "skill_match":
        return _skill_match(value, path)
    raise _bad(path, f"Unsupported operator {op}")  # pragma: no cover - guarded by the schema


# ---------------------------------------------------------------- predicates
def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _within(col: Any, fd: FieldDef, wd: WithinDays) -> ColumnElement[bool]:
    if fd.type == "date":
        today = func.current_date()
        if wd.direction == "past":
            return and_(col >= today - wd.days, col <= today)
        return and_(col >= today, col <= today + wd.days)
    now = func.now()
    span = func.make_interval(0, 0, 0, wd.days)
    if wd.direction == "past":
        return and_(col >= now - span, col <= now)
    return and_(col >= now, col <= now + span)


def predicate(fd: FieldDef, col: Any, op: str, value: Any) -> ColumnElement[bool]:
    """Build the SQL condition for a non-skill operator on ``col`` (``value`` already coerced)."""
    array = fd.type == "string_array"
    if op == "eq":
        return col.any_() == value if array else col == value
    if op == "ne":
        return col.is_distinct_from(value)
    if op == "in":
        return col.overlap(list(value)) if array else col.in_(value)
    if op == "not_in":
        return or_(col.not_in(value), col.is_(None))
    if op == "lt":
        return col < value
    if op == "lte":
        return col <= value
    if op == "gt":
        return col > value
    if op == "gte":
        return col >= value
    if op == "between":
        return col.between(value[0], value[1])
    if op == "contains":
        return col.ilike(f"%{_like_escape(value)}%", escape="\\")
    if op == "starts_with":
        return col.ilike(f"{_like_escape(value)}%", escape="\\")
    if op == "is_null":
        if array:
            null = or_(col.is_(None), func.cardinality(col) == 0)
            return null if value else not_(null)
        return col.is_(None) if value else col.is_not(None)
    if op == "within_days":
        return _within(col, fd, value)
    msg = f"no predicate for operator {op!r}"
    raise ValueError(msg)
