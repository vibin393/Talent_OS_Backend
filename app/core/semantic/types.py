"""Semantic layer value types (Epic 36 contract 0.6, AD-42/AD-46).

A *source* (``<domain>.<entity>``) names a root model, its subject column (the id a segment / pool is made
of), the fields a definition may reference and the joins those fields go through. Field expressions are
callables over an alias, so user input never becomes SQL text: definitions only pick registered keys.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from sqlalchemy import ColumnElement

from app.core.skill_refs import ExpandedSkill

__all__ = [
    "AGGS",
    "OPS",
    "Agg",
    "FieldDef",
    "FieldType",
    "JoinDef",
    "Op",
    "PiiClass",
    "ScopeSpec",
    "SourceDef",
]

PiiClass = Literal["none", "personal", "compensation", "identity", "protected"]
FieldType = Literal[
    "string", "int", "decimal", "bool", "date", "datetime", "uuid", "enum", "string_array", "skill"
]
Op = Literal[
    "eq",
    "ne",
    "in",
    "not_in",
    "lt",
    "lte",
    "gt",
    "gte",
    "between",
    "contains",
    "starts_with",
    "is_null",
    "within_days",
    "skill_match",
]
Agg = Literal["count", "count_distinct", "sum", "avg", "min", "max"]

PII_CLASSES: frozenset[str] = frozenset(get_args(PiiClass))
FIELD_TYPES: frozenset[str] = frozenset(get_args(FieldType))
OPS: frozenset[str] = frozenset(get_args(Op))
AGGS: frozenset[str] = frozenset(get_args(Agg))

# Shorthands for registrations ("cmp" in the 0.6 table = lt/lte/gt/gte).
CMP: frozenset[Op] = frozenset({"lt", "lte", "gt", "gte"})


@dataclass(frozen=True, slots=True)
class FieldDef:
    type: FieldType
    label: str
    pii_class: PiiClass
    # alias of the root (join=None) or of the join target -> column expression
    expr: Callable[[Any], ColumnElement[Any]]
    ops: frozenset[Op]  # empty = display only (not filterable)
    permission: str | None = None  # extra permission needed to use the field at all
    aggs: frozenset[Agg] = frozenset()
    groupable: bool = False
    date_buckets: bool = False
    enum_values: tuple[str, ...] | None = None
    join: str | None = None
    # type "skill": (alias, expanded skills, min_proficiency) -> boolean predicate
    skill_predicate: Callable[[Any, Sequence[ExpandedSkill], int | None], ColumnElement[bool]] | None = None


@dataclass(frozen=True, slots=True)
class JoinDef:
    target: Any  # model class or Table / subquery
    on: Callable[[Any, Any], ColumnElement[bool]]  # (root_alias, target_alias)
    many: bool = False  # one-to-many: filters only (EXISTS), never fields / group_by
    permission: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeSpec:
    permission: str  # source read permission
    owner_cols: tuple[Callable[[Any], Any], ...]  # OR-combined core.permissions.scope_condition
    bu_col: Callable[[Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class SourceDef:
    key: str
    root_model: Any
    fields: Mapping[str, FieldDef]
    joins: Mapping[str, JoinDef]
    scope: ScopeSpec
    label: str
    subject: Callable[[Any], Any]
    base_filter: Callable[[Any], ColumnElement[bool]] | None = None
