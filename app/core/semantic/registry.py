r"""Semantic source registry (Epic 36 contract 0.6).

Owners register from their ``hooks.py``. Every registration is validated here so a bad field fails the
process at import / startup (``SemanticRegistryError``), never a request:

* key ``<domain>.<entity>``; field keys ``^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$``
  (dotted = ``<join>.<field>``);
* ``pii_class`` present and never ``protected`` (AD-47: no fairness / protected data in any source);
* permissions are catalogue keys; joins exist; many-join fields are filter-only (not groupable, no aggs);
* operators / aggregates / buckets fit the field type.

``extend_semantic_source`` adds fields to an existing source; an extension of a source that is not registered
yet (hooks import alphabetically) is held and applied when the source registers.
"""

import re
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from app.core.permissions import CATALOG
from app.core.semantic.errors import SemanticRegistryError
from app.core.semantic.types import (
    AGGS,
    FIELD_TYPES,
    OPS,
    PII_CLASSES,
    FieldDef,
    JoinDef,
    ScopeSpec,
    SourceDef,
)

__all__ = [
    "OPS_BY_TYPE",
    "extend_semantic_source",
    "get_source",
    "register_semantic_source",
    "sources",
    "unregister_semantic_source",
]

SOURCE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")
JOIN_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ORDERED = frozenset({"eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte", "between", "is_null"})
OPS_BY_TYPE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "string": frozenset({"eq", "ne", "in", "not_in", "contains", "starts_with", "is_null"}),
        "int": _ORDERED,
        "decimal": _ORDERED,
        "bool": frozenset({"eq", "ne", "is_null"}),
        "date": _ORDERED | {"within_days"},
        "datetime": _ORDERED | {"within_days"},
        "uuid": frozenset({"eq", "ne", "in", "not_in", "is_null"}),
        "enum": frozenset({"eq", "ne", "in", "not_in", "is_null"}),
        "string_array": frozenset({"eq", "in", "is_null"}),
        "skill": frozenset({"skill_match"}),
    }
)
_NUMERIC = frozenset({"int", "decimal"})
_ORDERABLE = frozenset({"int", "decimal", "date", "datetime", "string"})

_lock = threading.Lock()
_sources: dict[str, SourceDef] = {}
_pending: dict[str, list[tuple[dict[str, FieldDef], dict[str, JoinDef]]]] = {}


def _fail(msg: str) -> SemanticRegistryError:
    return SemanticRegistryError(msg)


def _check_permission(where: str, permission: str | None, *, required: bool = False) -> None:
    if permission is None or permission == "":
        if required:
            raise _fail(f"{where}: permission is required")
        return
    if permission not in CATALOG:
        raise _fail(f"{where}: unknown permission {permission!r}")


def _check_join(source: str, name: str, join: Any) -> None:
    if not isinstance(join, JoinDef):
        raise _fail(f"{source}: join {name!r} must be a JoinDef")
    if not JOIN_KEY_RE.match(name):
        raise _fail(f"{source}: bad join key {name!r}")
    if join.target is None or not callable(join.on):
        raise _fail(f"{source}: join {name!r} needs a target and an on-clause")
    _check_permission(f"{source}.{name}", join.permission)


def _check_field(source: str, key: str, fd: Any, joins: Mapping[str, JoinDef]) -> None:
    where = f"{source}: field {key!r}"
    if not isinstance(fd, FieldDef):
        raise _fail(f"{where} must be a FieldDef")
    if not isinstance(key, str) or not FIELD_KEY_RE.match(key):
        raise _fail(f"{where}: bad field key")
    if getattr(fd, "pii_class", None) is None or fd.pii_class == "":  # type: ignore[comparison-overlap]
        raise _fail(f"{where}: pii_class is required")
    if fd.pii_class == "protected":
        raise _fail(f"{where}: pii_class 'protected' can never be registered (AD-47)")
    if fd.pii_class not in PII_CLASSES:
        raise _fail(f"{where}: unknown pii_class {fd.pii_class!r}")
    if fd.type not in FIELD_TYPES:
        raise _fail(f"{where}: unknown type {fd.type!r}")
    if not callable(fd.expr):
        raise _fail(f"{where}: expr must be callable")
    _check_permission(where, fd.permission)
    if "." in key:
        prefix = key.split(".", 1)[0]
        if prefix not in joins:
            raise _fail(f"{where}: unknown join {prefix!r}")
        if fd.join != prefix:
            raise _fail(f"{where}: dotted key must use join {prefix!r}")
    join = None
    if fd.join is not None:
        join = joins.get(fd.join)
        if join is None:
            raise _fail(f"{where}: unknown join {fd.join!r}")
    unknown_ops = set(fd.ops) - OPS  # type: ignore[operator]
    if unknown_ops:
        raise _fail(f"{where}: unknown operators {sorted(unknown_ops)}")
    bad_ops = set(fd.ops) - OPS_BY_TYPE[fd.type]  # type: ignore[operator]
    if bad_ops:
        raise _fail(f"{where}: operators {sorted(bad_ops)} do not fit type {fd.type!r}")
    unknown_aggs = set(fd.aggs) - AGGS  # type: ignore[operator]
    if unknown_aggs:
        raise _fail(f"{where}: unknown aggregates {sorted(unknown_aggs)}")
    if {"sum", "avg"} & set(fd.aggs) and fd.type not in _NUMERIC:
        raise _fail(f"{where}: sum/avg need a numeric field")
    if {"min", "max"} & set(fd.aggs) and fd.type not in _ORDERABLE:
        raise _fail(f"{where}: min/max need an orderable field")
    if fd.type == "enum" and not fd.enum_values:
        raise _fail(f"{where}: enum fields need enum_values")
    if fd.type == "skill" and fd.skill_predicate is None:
        raise _fail(f"{where}: skill fields need a skill_predicate")
    if fd.type == "skill" and (fd.groupable or fd.aggs):
        raise _fail(f"{where}: skill fields are filter-only")
    if fd.date_buckets and fd.type not in {"date", "datetime"}:
        raise _fail(f"{where}: date_buckets need a date / datetime field")
    if join is not None and join.many and (fd.groupable or fd.aggs or fd.date_buckets):
        raise _fail(f"{where}: fields of a many-join are filter-only (not groupable, no aggregates)")
    if join is not None and join.many and not fd.ops:
        raise _fail(f"{where}: a many-join field must be filterable")


def _check_scope(source: str, scope: Any) -> None:
    if not isinstance(scope, ScopeSpec):
        raise _fail(f"{source}: scope_resource must be a ScopeSpec")
    _check_permission(f"{source}: scope", scope.permission, required=True)
    if not scope.owner_cols or not all(callable(c) for c in scope.owner_cols):
        raise _fail(f"{source}: scope needs at least one owner column")


def _validated(
    key: str, fields: Mapping[str, FieldDef], joins: Mapping[str, JoinDef]
) -> tuple[dict[str, FieldDef], dict[str, JoinDef]]:
    for name, join in joins.items():
        _check_join(key, name, join)
    for fkey, fd in fields.items():
        _check_field(key, fkey, fd, joins)
    return dict(fields), dict(joins)


def _apply_extension(src: SourceDef, fields: dict[str, FieldDef], joins: dict[str, JoinDef]) -> SourceDef:
    clash = (set(fields) & set(src.fields)) | (set(joins) & set(src.joins))
    if clash:
        raise _fail(f"{src.key}: duplicate keys {sorted(clash)}")
    all_joins = {**src.joins, **joins}
    _validated(src.key, fields, all_joins)
    return SourceDef(
        key=src.key,
        root_model=src.root_model,
        fields=MappingProxyType({**src.fields, **fields}),
        joins=MappingProxyType(all_joins),
        scope=src.scope,
        label=src.label,
        subject=src.subject,
        base_filter=src.base_filter,
    )


def register_semantic_source(
    key: str,
    root_model: Any,
    fields: Mapping[str, FieldDef],
    joins: Mapping[str, JoinDef],
    scope_resource: ScopeSpec,
    *,
    label: str,
    subject: Any,
    base_filter: Any = None,
) -> None:
    if not isinstance(key, str) or not SOURCE_KEY_RE.match(key):
        raise _fail(f"bad source key {key!r} (expected '<domain>.<entity>')")
    if root_model is None or not callable(subject):
        raise _fail(f"{key}: root_model and subject are required")
    if base_filter is not None and not callable(base_filter):
        raise _fail(f"{key}: base_filter must be callable")
    _check_scope(key, scope_resource)
    checked_fields, checked_joins = _validated(key, fields, joins)
    with _lock:
        if key in _sources:
            raise _fail(f"duplicate semantic source {key!r}")
        src = SourceDef(
            key=key,
            root_model=root_model,
            fields=MappingProxyType(checked_fields),
            joins=MappingProxyType(checked_joins),
            scope=scope_resource,
            label=label,
            subject=subject,
            base_filter=base_filter,
        )
        for ext_fields, ext_joins in _pending.pop(key, []):
            src = _apply_extension(src, ext_fields, ext_joins)
        _sources[key] = src


def extend_semantic_source(
    key: str, *, fields: Mapping[str, FieldDef], joins: Mapping[str, JoinDef] = MappingProxyType({})
) -> None:
    """Add fields (and their joins) to a source; held until the source registers if it is not there yet."""
    if not isinstance(key, str) or not SOURCE_KEY_RE.match(key):
        raise _fail(f"bad source key {key!r} (expected '<domain>.<entity>')")
    with _lock:
        src = _sources.get(key)
        if src is None:
            # Pre-check what can be checked without the base source's joins.
            for name, join in joins.items():
                _check_join(key, name, join)
            _pending.setdefault(key, []).append((dict(fields), dict(joins)))
            return
        _sources[key] = _apply_extension(src, dict(fields), dict(joins))


def get_source(key: str) -> SourceDef:
    """Return the registered source (``KeyError`` when unknown)."""
    return _sources[key]


def sources() -> Mapping[str, SourceDef]:
    return MappingProxyType(dict(_sources))


def unregister_semantic_source(key: str) -> None:
    """Test helper: drop a source (and pending extensions)."""
    with _lock:
        _sources.pop(key, None)
        _pending.pop(key, None)
