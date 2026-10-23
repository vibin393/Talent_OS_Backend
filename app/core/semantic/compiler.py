"""Definition validation and compilation (Epic 36 contract 0.6, AD-42/AD-46).

``validate`` parses a definition and checks it against the registry (and, given a principal, the field
permissions); ``compile`` turns it into a ``select()`` over ``aliased(root_model)``:

1. principal check (internal ``user`` / ``api_key`` only; ``SemanticContext.for_system`` skips scope and field
   permissions) and the source read permission (403);
2. field permissions / pii rules: a field (or its join) with a ``permission`` not held -> 422
   ``semantic_field_forbidden``; ``compensation`` / ``identity`` fields without ``compensation:view`` /
   ``identity:view`` -> 422 when filtered, grouped or measured, ``null`` + ``masked_fields`` when selected;
3. LEFT OUTER joins for used one-joins; the filter tree with **one correlated EXISTS per many-join group** of
   sibling conditions in an ``and`` node; ``base_filter``; the scope condition OR-ed over ``owner_cols``;
4. fields (row mode) or group_by (date buckets; ``fy`` = April-start, labelled via ``core.fiscal``) and
   measures (``ratio`` = m1 / NULLIF(m2, 0); ``pct_of_total`` = 100 * measure / SUM(measure) OVER ());
   sort; default order by subject id.

Only registered keys choose SQL; every user value is a bound parameter. Output columns are labelled ``c<n>`` /
``g<n>`` / ``m<n>`` and GROUP BY uses ordinals, so no user text ever reaches the SQL string.
"""

import dataclasses
import uuid
from collections.abc import Mapping
from typing import Any, Literal, get_args

from pydantic import ValidationError
from sqlalchemy import (
    ColumnElement,
    Date,
    Numeric,
    Select,
    and_,
    cast,
    distinct,
    exists,
    func,
    literal_column,
    null,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.core.permissions import COMPENSATION_VIEW, Principal, forbidden, scope_condition
from app.core.semantic import errors as E  # ruff: ignore[lowercase-imported-as-non-lowercase]
from app.core.semantic.errors import SemanticError
from app.core.semantic.operators import coerce_value, expand_skills, predicate
from app.core.semantic.registry import get_source
from app.core.semantic.schema import (
    MAX_CONDITIONS,
    MAX_DEPTH,
    MAX_FIELDS,
    MAX_GROUP_BY,
    MAX_MEASURES,
    MAX_SORT,
    AndNode,
    Condition,
    Definition,
    OrNode,
)
from app.core.semantic.types import FieldDef, JoinDef, SourceDef
from app.schemas.semantic import ColumnMeta

__all__ = [
    "IDENTITY_VIEW",
    "INTERNAL_PRINCIPALS",
    "PII_PERMISSIONS",
    "PURPOSES",
    "CompiledQuery",
    "Purpose",
    "SemanticContext",
    "compile",
    "validate",
]

Purpose = Literal["segment", "report", "eligibility", "audience", "refresh"]
PURPOSES: frozenset[str] = frozenset(get_args(Purpose))
IDENTITY_VIEW = "identity:view"
INTERNAL_PRINCIPALS: frozenset[str] = frozenset({"user", "api_key", "app"})  # Epic 66: +app
PII_PERMISSIONS = {"compensation": COMPENSATION_VIEW, "identity": IDENTITY_VIEW}
_NODE_TAGS = frozenset({"condition", "and", "or"})
_TRUNC = {
    "day": literal_column("'day'"),
    "week": literal_column("'week'"),
    "month": literal_column("'month'"),
    "quarter": literal_column("'quarter'"),
}
_FY_SHIFT = literal_column("interval '3 months'")


@dataclasses.dataclass(frozen=True, slots=True)
class SemanticContext:
    request: RequestContext
    db: AsyncSession
    purpose: Purpose
    # Set only by ``for_system`` (tasks): no principal, no scope, no field permissions.
    system: bool = dataclasses.field(default=False, kw_only=True)

    @classmethod
    def for_system(cls, tenant_id: uuid.UUID, db: AsyncSession, purpose: str) -> "SemanticContext":
        """Build a system context for tasks (pool refresh, eligibility): no scope or field permissions. Never
        build        it from a request."""
        if purpose not in PURPOSES:
            msg = f"unknown semantic purpose {purpose!r}"
            raise ValueError(msg)
        request = RequestContext(tenant_id=tenant_id, principal=None, actor_kind="system")
        return cls(request, db, purpose, system=True)  # type: ignore[arg-type]


@dataclasses.dataclass(frozen=True, slots=True)
class CompiledQuery:
    stmt: Select[Any]
    columns: list[ColumnMeta]
    masked_fields: list[str]
    subject_expr: ColumnElement[Any]
    # Internals for the executor.
    source: str = ""
    labels: Mapping[str, str] = dataclasses.field(default_factory=dict)  # SQL label -> output key
    fy_labels: frozenset[str] = frozenset()  # labels whose dates become "YY-YY"
    ids_stmt: Select[Any] | None = None
    count_stmt: Select[Any] | None = None


# ================================================================ validation
@dataclasses.dataclass(slots=True)
class _Plan:
    src: SourceDef
    aggregate: bool
    masked: set[str]


def _raw_too_deep(raw: Any) -> bool:
    """Pre-scan a raw filter tree iteratively (guards pydantic against pathological nesting)."""
    stack: list[tuple[Any, int]] = [(raw, 1)]
    seen = 0
    while stack:
        node, depth = stack.pop()
        seen += 1
        if depth > MAX_DEPTH + 1 or seen > MAX_CONDITIONS * 4:
            return True
        if isinstance(node, Mapping):
            for k in ("and", "or", "and_", "or_"):
                children = node.get(k)
                if isinstance(children, list):
                    stack.extend((c, depth + 1) for c in children)
    return False


def _loc_path(loc: tuple[Any, ...]) -> str:
    out = ""
    skip_tag = False
    for part in loc:
        if skip_tag and part in _NODE_TAGS:
            skip_tag = False
            continue
        skip_tag = False
        if isinstance(part, int):
            out += f"[{part}]"
            skip_tag = True
        else:
            name = {"and_": "and", "or_": "or"}.get(str(part), str(part))
            out = f"{out}.{name}" if out else name
            if name == "filters":
                skip_tag = True
    return out


def _from_pydantic(exc: ValidationError) -> SemanticError:
    err = exc.errors()[0]
    loc = tuple(err.get("loc", ()))
    path = _loc_path(loc)
    code = E.VALUE_TYPE
    if err.get("type") == "literal_error" and loc and loc[-1] in {"op", "agg"}:
        code = E.OPERATOR_NOT_ALLOWED
    return SemanticError(code, str(err.get("msg", "Invalid definition")), path)


def _parse(defn: Mapping[str, Any] | Definition) -> Definition:
    if isinstance(defn, Definition):
        return defn
    if not isinstance(defn, Mapping):
        raise SemanticError(E.VALUE_TYPE, "A definition must be an object", "")
    if "filters" in defn and _raw_too_deep(defn["filters"]):
        raise SemanticError(E.TOO_COMPLEX, f"Filters are limited to depth {MAX_DEPTH}", "filters")
    for key, limit in (
        ("fields", MAX_FIELDS),
        ("group_by", MAX_GROUP_BY),
        ("measures", MAX_MEASURES),
        ("sort", MAX_SORT),
    ):
        value = defn.get(key)
        if isinstance(value, list) and len(value) > limit:
            raise SemanticError(E.TOO_COMPLEX, f"At most {limit} {key} entries", key)
    try:
        return Definition.model_validate(dict(defn))
    except ValidationError as exc:
        raise _from_pydantic(exc) from None


def _resolve(src: SourceDef, key: str, path: str) -> tuple[FieldDef, JoinDef | None]:
    fd = src.fields.get(key)
    if fd is None:
        if "." in key and key.split(".", 1)[0] not in src.joins:
            raise SemanticError(E.UNKNOWN_JOIN, f"Unknown join in {key!r}", path)
        raise SemanticError(E.UNKNOWN_FIELD, f"Unknown field {key!r}", path)
    return fd, (src.joins[fd.join] if fd.join is not None else None)


def _access(
    key: str, fd: FieldDef, join: JoinDef | None, principal: Principal | None, path: str, *, display: bool
) -> bool:
    """Return True when the field must be masked (display only); raise 422 when it may not be used."""
    if principal is None:
        return False
    for permission in (fd.permission, join.permission if join is not None else None):
        if permission is not None and not principal.has(permission):
            raise SemanticError(
                E.FIELD_FORBIDDEN, f"You may not use {key!r}", path, required_permission=permission
            )
    pii_permission = PII_PERMISSIONS.get(fd.pii_class)
    if pii_permission is not None and not principal.has(pii_permission):
        if display:
            return True
        raise SemanticError(
            E.FIELD_FORBIDDEN,
            f"You may not filter or aggregate on {key!r}",
            path,
            required_permission=pii_permission,
        )
    return False


def _walk_filters(
    src: SourceDef, node: Any, path: str, principal: Principal | None, depth: int, counter: list[int]
) -> None:
    if depth > MAX_DEPTH:
        raise SemanticError(E.TOO_COMPLEX, f"Filters are limited to depth {MAX_DEPTH}", path)
    if isinstance(node, Condition):
        counter[0] += 1
        if counter[0] > MAX_CONDITIONS:
            raise SemanticError(E.TOO_COMPLEX, f"At most {MAX_CONDITIONS} conditions", path)
        fd, join = _resolve(src, node.field, f"{path}.field")
        if node.op not in fd.ops:
            raise SemanticError(
                E.OPERATOR_NOT_ALLOWED, f"{node.op!r} is not allowed on {node.field!r}", f"{path}.op"
            )
        _access(node.field, fd, join, principal, f"{path}.field", display=False)
        coerce_value(fd, node.op, node.value, f"{path}.value")
        return
    name, children = ("and", node.and_) if isinstance(node, AndNode) else ("or", node.or_)
    for i, child in enumerate(children):
        _walk_filters(src, child, f"{path}.{name}[{i}]", principal, depth + 1, counter)


def _check_selectable(key: str, fd: FieldDef, join: JoinDef | None, path: str) -> None:
    if (join is not None and join.many) or fd.type == "skill":
        raise SemanticError(E.OPERATOR_NOT_ALLOWED, f"{key!r} can only be used in filters", path)


def _analyze(defn: Definition, principal: Principal | None) -> _Plan:
    try:
        src = get_source(defn.source)
    except KeyError:
        raise SemanticError(E.UNKNOWN_SOURCE, f"Unknown source {defn.source!r}", "source") from None
    for key, limit in (
        ("fields", MAX_FIELDS),
        ("group_by", MAX_GROUP_BY),
        ("measures", MAX_MEASURES),
        ("sort", MAX_SORT),
    ):
        if len(getattr(defn, key)) > limit:
            raise SemanticError(E.TOO_COMPLEX, f"At most {limit} {key} entries", key)
    plan = _Plan(src=src, aggregate=bool(defn.group_by or defn.measures), masked=set())
    if defn.filters is not None:
        _walk_filters(src, defn.filters, "filters", principal, 1, [0])
    if plan.aggregate and defn.fields:
        raise SemanticError(
            E.OPERATOR_NOT_ALLOWED, "fields cannot be combined with group_by / measures", "fields"
        )
    for i, key in enumerate(defn.fields):
        fd, join = _resolve(src, key, f"fields[{i}]")
        _check_selectable(key, fd, join, f"fields[{i}]")
        if _access(key, fd, join, principal, f"fields[{i}]", display=True):
            plan.masked.add(key)
    seen_groups: set[str] = set()
    for i, g in enumerate(defn.group_by):
        path = f"group_by[{i}].field"
        fd, join = _resolve(src, g.field, path)
        _check_selectable(g.field, fd, join, path)
        if not fd.groupable:
            raise SemanticError(E.OPERATOR_NOT_ALLOWED, f"{g.field!r} cannot be grouped", path)
        if g.bucket is not None and not fd.date_buckets:
            raise SemanticError(
                E.OPERATOR_NOT_ALLOWED, f"{g.field!r} has no date buckets", f"group_by[{i}].bucket"
            )
        if g.field in seen_groups:
            raise SemanticError(E.VALUE_TYPE, f"{g.field!r} is grouped twice", path)
        seen_groups.add(g.field)
        _access(g.field, fd, join, principal, path, display=False)
    measure_keys: set[str] = set()
    for i, m in enumerate(defn.measures):
        path = f"measures[{i}]"
        if m.key in measure_keys or m.key in seen_groups:
            raise SemanticError(E.VALUE_TYPE, f"Duplicate key {m.key!r}", f"{path}.key")
        if m.agg == "ratio":
            if m.of is None or m.field is not None or not set(m.of) <= measure_keys:
                raise SemanticError(
                    E.VALUE_TYPE, "ratio needs of=[m1, m2] naming earlier measures", f"{path}.of"
                )
        else:
            if m.of is not None:
                raise SemanticError(E.VALUE_TYPE, "of is only used by ratio", f"{path}.of")
            if m.field is None:
                if m.agg not in {"count", "pct_of_total"}:
                    raise SemanticError(E.VALUE_TYPE, f"{m.agg} needs a field", f"{path}.field")
            else:
                fd, join = _resolve(src, m.field, f"{path}.field")
                _check_selectable(m.field, fd, join, f"{path}.field")
                needed = "sum" if m.agg == "pct_of_total" else m.agg
                if needed not in fd.aggs:
                    raise SemanticError(
                        E.OPERATOR_NOT_ALLOWED, f"{m.agg} is not allowed on {m.field!r}", f"{path}.agg"
                    )
                _access(m.field, fd, join, principal, f"{path}.field", display=False)
        measure_keys.add(m.key)
    sortable = (seen_groups | measure_keys) if plan.aggregate else set(defn.fields)
    for i, s in enumerate(defn.sort):
        if s.key not in sortable:
            raise SemanticError(E.UNKNOWN_FIELD, f"Sort key {s.key!r} is not selected", f"sort[{i}].key")
        if s.key in plan.masked:
            raise SemanticError(E.FIELD_FORBIDDEN, f"You may not sort on {s.key!r}", f"sort[{i}].key")
    return plan


def validate(defn: Mapping[str, Any] | Definition, *, principal: Principal | None = None) -> Definition:
    """Parse and check a definition against the registry (and the principal's field permissions)."""
    parsed = _parse(defn)
    _analyze(parsed, principal)
    return parsed


# ================================================================ compilation
def _alias(target: Any) -> Any:
    return aliased(target) if isinstance(target, type) else target.alias()


class _Builder:
    def __init__(self, src: SourceDef, ctx: SemanticContext) -> None:
        self.src = src
        self.ctx = ctx
        self.root = aliased(src.root_model)
        self.one_joins: dict[str, Any] = {}

    def target(self, join_name: str) -> Any:
        alias = self.one_joins.get(join_name)
        if alias is None:
            alias = _alias(self.src.joins[join_name].target)
            self.one_joins[join_name] = alias
        return alias

    def expr(self, fd: FieldDef) -> Any:
        return fd.expr(self.target(fd.join) if fd.join is not None else self.root)

    async def condition(self, cond: Condition, alias: Any) -> ColumnElement[bool]:
        fd = self.src.fields[cond.field]
        value = coerce_value(fd, cond.op, cond.value, "value")
        if cond.op == "skill_match":
            assert fd.skill_predicate is not None
            expanded = await expand_skills(self.ctx.request, self.ctx.db, value)
            return fd.skill_predicate(alias, expanded, value.min_proficiency)
        return predicate(fd, fd.expr(alias), cond.op, value)

    def _many(self, cond: Condition) -> str | None:
        fd = self.src.fields[cond.field]
        if fd.join is not None and self.src.joins[fd.join].many:
            return fd.join
        return None

    async def exists(self, join_name: str, conds: list[Condition]) -> ColumnElement[bool]:
        jd = self.src.joins[join_name]
        t = _alias(jd.target)
        preds = [jd.on(self.root, t)]
        preds.extend([await self.condition(c, t) for c in conds])
        return exists(select(literal_column("1")).select_from(t).where(and_(*preds)))

    async def node(self, node: Any) -> ColumnElement[bool]:
        if isinstance(node, Condition):
            many = self._many(node)
            if many is not None:
                return await self.exists(many, [node])
            fd = self.src.fields[node.field]
            alias = self.target(fd.join) if fd.join is not None else self.root
            return await self.condition(node, alias)
        if isinstance(node, OrNode):
            return or_(*[await self.node(c) for c in node.or_])
        assert isinstance(node, AndNode)
        parts: list[ColumnElement[bool]] = []
        groups: dict[str, list[Condition]] = {}
        for child in node.and_:
            many = self._many(child) if isinstance(child, Condition) else None
            if many is not None:
                groups.setdefault(many, []).append(child)
            else:
                parts.append(await self.node(child))
        for join_name, conds in groups.items():
            parts.append(await self.exists(join_name, conds))
        return and_(*parts)

    def scope(self) -> ColumnElement[bool] | None:
        principal = self.ctx.request.principal
        spec = self.src.scope
        conditions = []
        for owner in spec.owner_cols:
            cond = scope_condition(
                principal, spec.permission, owner(self.root), spec.bu_col(self.root) if spec.bu_col else None
            )
            if cond is None:
                return None
            conditions.append(cond)
        return or_(*conditions) if len(conditions) > 1 else conditions[0]

    def frame(self, cols: list[Any], where: list[ColumnElement[bool]]) -> Select[Any]:
        stmt = select(*cols).select_from(self.root)
        for name, alias in self.one_joins.items():
            stmt = stmt.outerjoin(alias, self.src.joins[name].on(self.root, alias))
        return stmt.where(*where) if where else stmt


def _bucket(expr: Any, bucket: str | None) -> Any:
    if bucket is None:
        return expr
    if bucket == "fy":
        return cast(func.date_trunc(literal_column("'year'"), expr - _FY_SHIFT) + _FY_SHIFT, Date)
    return cast(func.date_trunc(_TRUNC[bucket], expr), Date)


def _measure_expr(b: _Builder, src: SourceDef, m: Any, done: Mapping[str, Any]) -> tuple[Any, str]:
    if m.agg == "ratio":
        m1, m2 = m.of
        return cast(done[m1], Numeric) / func.nullif(done[m2], 0), "decimal"
    if m.agg == "pct_of_total":
        base = func.count() if m.field is None else func.sum(b.expr(src.fields[m.field]))
        return cast(base, Numeric) * 100 / func.nullif(func.sum(base).over(), 0), "decimal"
    if m.field is None:
        return func.count(), "int"
    fd = src.fields[m.field]
    e = b.expr(fd)
    if m.agg == "count":
        return func.count(e), "int"
    if m.agg == "count_distinct":
        return func.count(distinct(e)), "int"
    if m.agg == "avg":
        return func.avg(e), "decimal"
    return getattr(func, m.agg)(e), fd.type


def _check_principal(ctx: SemanticContext, src: SourceDef) -> Principal | None:
    if ctx.system:
        return None
    principal = ctx.request.principal
    if principal is None or getattr(principal, "type", None) not in INTERNAL_PRINCIPALS:
        raise Forbidden("Semantic queries are available to internal users only")
    if not principal.has(src.scope.permission):
        raise forbidden(src.scope.permission)
    return principal


async def compile(defn: Definition, ctx: SemanticContext) -> CompiledQuery:
    """Compile a definition for ``ctx`` (raises 403 / 422)."""
    defn = _parse(defn)
    try:
        src = get_source(defn.source)
    except KeyError:
        raise SemanticError(E.UNKNOWN_SOURCE, f"Unknown source {defn.source!r}", "source") from None
    principal = _check_principal(ctx, src)
    plan = _analyze(defn, principal)
    b = _Builder(src, ctx)
    root = b.root
    subject = src.subject(root)

    where: list[ColumnElement[bool]] = []
    tenant_col = getattr(root, "tenant_id", None)
    if tenant_col is not None and ctx.request.tenant_id is not None:
        where.append(tenant_col == ctx.request.tenant_id)
    if defn.filters is not None:
        where.append(await b.node(defn.filters))
    if src.base_filter is not None:
        where.append(src.base_filter(root))
    if principal is not None:
        scope = b.scope()
        if scope is not None:
            where.append(scope)

    columns: list[ColumnMeta] = []
    labels: dict[str, str] = {}
    fy_labels: set[str] = set()
    sort_targets: dict[str, Any] = {}
    if plan.aggregate:
        cols: list[Any] = []
        group_ordinals = []
        for i, g in enumerate(defn.group_by):
            fd = src.fields[g.field]
            label = f"g{i}"
            col = _bucket(b.expr(fd), g.bucket).label(label)
            cols.append(col)
            group_ordinals.append(literal_column(str(i + 1)))
            labels[label] = g.field
            if g.bucket == "fy":
                fy_labels.add(label)
            col_type = "string" if g.bucket == "fy" else ("date" if g.bucket else fd.type)
            columns.append(ColumnMeta(key=g.field, label=fd.label, type=col_type))
            sort_targets[g.field] = col
        measures = defn.measures or []
        done: dict[str, Any] = {}
        if not measures:
            cols.append(func.count().label("m0"))
            labels["m0"] = "count"
            columns.append(ColumnMeta(key="count", label="Count", type="int"))
        for i, m in enumerate(measures):
            expr, col_type = _measure_expr(b, src, m, done)
            done[m.key] = expr
            label = f"m{i}"
            col = expr.label(label)
            cols.append(col)
            labels[label] = m.key
            columns.append(ColumnMeta(key=m.key, label=m.key, type=col_type))
            sort_targets[m.key] = col
        stmt = b.frame(cols, where)
        if group_ordinals:
            stmt = stmt.group_by(*group_ordinals)
        order = [sort_targets[s.key].desc() if s.desc else sort_targets[s.key] for s in defn.sort]
        order.extend(c for c in cols[: len(defn.group_by)])
        stmt = stmt.order_by(*order) if order else stmt
    else:
        subject_col = subject.label("subject_id")
        cols = [subject_col]
        for i, key in enumerate(defn.fields):
            fd = src.fields[key]
            label = f"c{i}"
            col = null().label(label) if key in plan.masked else b.expr(fd).label(label)
            cols.append(col)
            labels[label] = key
            columns.append(ColumnMeta(key=key, label=fd.label, type=fd.type, masked=key in plan.masked))
            sort_targets.setdefault(key, col)
        stmt = b.frame(cols, where)
        order = [
            sort_targets[s.key].desc().nulls_last() if s.desc else sort_targets[s.key].nulls_last()
            for s in defn.sort
        ]
        order.append(subject_col)
        root_id = getattr(root, "id", None)
        if root_id is not None:
            order.append(root_id)
        stmt = stmt.order_by(*order)

    ids_col = subject.label("subject_id")
    ids_stmt = b.frame([ids_col], where).distinct().order_by(ids_col)
    count_stmt = b.frame([func.count(distinct(subject))], where)
    return CompiledQuery(
        stmt=stmt,
        columns=columns,
        masked_fields=sorted(plan.masked),
        subject_expr=subject,
        source=src.key,
        labels=labels,
        fy_labels=frozenset(fy_labels),
        ids_stmt=ids_stmt,
        count_stmt=count_stmt,
    )
