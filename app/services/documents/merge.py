"""Merge-field catalogue, merge-source registry and the sandboxed renderer (Epic 22 A2, FR-117).

* ``MERGE_CATALOGUE``: namespace -> allowed attributes. ``today`` is a bare value. Never Aadhaar numbers.
* ``register_merge_source(namespace, loader, ref_key=...)``: ``loader(db, ref_id) -> dict``. ``documents`` never
  imports placements / commercials (AD-32): Epic 19 registers ``worker``, ``placement``, ``rate_segment``,
  ``client`` and ``legal_entity`` (ref key ``placement_id``) from its hooks. Callers may pass ready
  ``merge_data`` (a ``MergeContext``) instead.
* Rendering: Jinja2 ``SandboxedEnvironment`` + ``StrictUndefined`` + autoescape; every ``ns.attr`` is checked
  against the catalogue (AST walk); filters are allow-listed (``date``, ``money``, ``upper``).
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError, nodes
from jinja2.sandbox import SandboxedEnvironment, SecurityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed
from app.core.registry import Registry

MERGE_CATALOGUE: dict[str, tuple[str, ...]] = {
    "worker": ("full_name", "email", "phone", "worker_id", "address"),
    "placement": ("id", "role", "start_date", "end_date", "location", "worker_type"),
    "client": ("name", "address"),
    "legal_entity": ("name", "address", "pan_last4", "gstin"),
    "rate_segment": ("pay_rate", "bill_rate", "currency", "rate_unit", "effective_from"),
    "tenant": ("name",),
    "signatory": ("name", "title"),
    "today": (),
}
FILTERS = ("date", "money", "upper")

SAMPLE_CONTEXT: dict[str, Any] = {
    "worker": {
        "full_name": "Asha Rao",
        "email": "asha.rao@example.com",
        "phone": "+91 98765 43210",
        "worker_id": "CAN-000123",
        "address": "12 MG Road, Bengaluru 560001",
    },
    "placement": {
        "id": "PLC-0001",
        "role": "Senior Data Engineer",
        "start_date": date(2026, 11, 2),
        "end_date": date(2027, 4, 30),
        "location": "Chennai",
        "worker_type": "contractor",
    },
    "client": {"name": "Globex Corporation", "address": "1 Park Street, Kolkata 700016"},
    "legal_entity": {
        "name": "Initech Talent Pvt Ltd",
        "address": "4 Residency Road, Bengaluru 560025",
        "pan_last4": "123F",
        "gstin": "29ABCDE1234F1Z5",
    },
    "rate_segment": {
        "pay_rate": Decimal("1200.00"),
        "bill_rate": Decimal("1650.00"),
        "currency": "INR",
        "rate_unit": "hour",
        "effective_from": date(2026, 11, 2),
    },
    "tenant": {"name": "Initech Talent"},
    "signatory": {"name": "Meera Iyer", "title": "Head of Delivery"},
    "today": date(2026, 10, 1),
}

MergeLoader = Callable[[AsyncSession, uuid.UUID], Awaitable[Mapping[str, Any]]]


@dataclasses.dataclass(frozen=True, slots=True)
class MergeSource:
    namespace: str
    loader: MergeLoader
    ref_key: str


_sources: Registry[str, MergeSource] = Registry("merge source", allow_replace=True)


def register_merge_source(namespace: str, loader: MergeLoader, *, ref_key: str | None = None) -> None:
    """Register the loader of ``namespace``; ``ref_key`` names the ``merge_refs`` entry passed as ``ref_id``
    (default ``<namespace>_id``)."""
    if namespace not in MERGE_CATALOGUE or namespace == "today":
        msg = f"unknown merge namespace {namespace!r}"
        raise ValueError(msg)
    _sources.register(namespace, MergeSource(namespace, loader, ref_key or f"{namespace}_id"))


def merge_sources() -> Mapping[str, MergeSource]:
    return _sources.view()


# ================================================================ filters / environment
def _date(value: Any, fmt: str = "%d %b %Y") -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime | date):
        return value.strftime(fmt)
    try:
        return date.fromisoformat(str(value)[:10]).strftime(fmt)
    except ValueError:
        return str(value)


def _money(value: Any, currency: str = "INR") -> str:
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    return f"{currency} {amount:,.2f}"


def _upper(value: Any) -> str:
    return str(value or "").upper()


def _env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True)
    env.filters = {"date": _date, "money": _money, "upper": _upper}  # allow-list only
    env.tests = {}
    env.globals = {}
    return env


_ENV = _env()


# ================================================================ static check
def _dotted(node: nodes.Node) -> str | None:
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.Getattr):
        base = _dotted(node.node)
        return f"{base}.{node.attr}" if base else None
    return None


def referenced_fields(body: str) -> list[str]:
    """Every data reference of the template (``ns.attr`` or ``today``); 422 on syntax errors, calls,
    subscripts or filters outside the allow-list."""
    try:
        tree = _ENV.parse(body)
    except TemplateSyntaxError as exc:
        raise ValidationFailed(
            "The template has a syntax error", code="template_syntax_error", details={"line": exc.lineno}
        ) from exc
    bad: list[str] = []
    for f in tree.find_all(nodes.Filter):
        if f.name not in FILTERS:
            bad.append(f"|{f.name}")
    for _ in tree.find_all((nodes.Call, nodes.Getitem, nodes.Test)):
        bad.append("<expression>")
    for _ in tree.find_all(
        (nodes.For, nodes.Assign, nodes.AssignBlock, nodes.Macro, nodes.Import, nodes.Include)
    ):
        bad.append("<statement>")
    inner = {id(n.node) for n in tree.find_all(nodes.Getattr)}
    found: list[str] = []
    for node in tree.find_all((nodes.Getattr, nodes.Name)):
        if id(node) in inner or (isinstance(node, nodes.Name) and node.ctx != "load"):
            continue
        dotted = _dotted(node)
        if dotted is None:
            bad.append("<expression>")
            continue
        found.append(dotted)
    if bad:
        raise ValidationFailed(
            "Only merge fields can be used in a template",
            code="unknown_merge_field",
            details={"fields": sorted(set(bad))},
        )
    return found


def unknown_fields(fields: Iterable[str]) -> list[str]:
    out: list[str] = []
    for field in fields:
        ns, _, attr = field.partition(".")
        if ns not in MERGE_CATALOGUE:
            out.append(field)
        elif ns == "today":
            if attr:
                out.append(field)
        elif attr not in MERGE_CATALOGUE[ns]:
            out.append(field)
    return sorted(set(out))


def check_fields(body: str) -> list[str]:
    """Validate the body; returns the sorted merge fields used. 422 ``unknown_merge_field`` (``details.fields``)."""
    fields = referenced_fields(body)
    unknown = unknown_fields(fields)
    if unknown:
        raise ValidationFailed(
            f"Unknown merge field: {', '.join(unknown)}",
            code="unknown_merge_field",
            details={"fields": unknown},
        )
    return sorted(set(fields))


# ================================================================ context
def _blank() -> dict[str, Any]:
    out: dict[str, Any] = {ns: dict.fromkeys(attrs, "") for ns, attrs in MERGE_CATALOGUE.items() if attrs}
    out["today"] = datetime.now(UTC).date()
    return out


async def build_context(
    db: AsyncSession,
    fields: Iterable[str],
    *,
    merge_refs: Mapping[str, uuid.UUID] | None = None,
    merge_data: Mapping[str, Any] | None = None,
    tenant_name: str | None = None,
) -> dict[str, Any]:
    """The merge context for ``fields``: ``merge_data`` first, then registered sources (by ``merge_refs``).
    A namespace that is used but cannot be resolved -> 422 ``merge_source_missing``."""
    ctx = _blank()
    if tenant_name:
        ctx["tenant"]["name"] = tenant_name
    provided: set[str] = {"today"} | ({"tenant"} if tenant_name else set())
    for ns, value in (merge_data or {}).items():
        if ns not in MERGE_CATALOGUE:
            continue
        if ns == "today":
            ctx["today"] = value
        elif isinstance(value, Mapping):
            ctx[ns].update({k: v for k, v in value.items() if k in MERGE_CATALOGUE[ns]})
        provided.add(ns)
    used = {f.partition(".")[0] for f in fields}
    missing: list[str] = []
    refs = dict(merge_refs or {})
    for ns in sorted(used - provided):
        source = _sources.view().get(ns)
        ref = refs.get(source.ref_key) if source else None
        if source is None or ref is None:
            missing.append(ns)
            continue
        loaded = await source.loader(db, ref)
        ctx[ns].update({k: v for k, v in loaded.items() if k in MERGE_CATALOGUE[ns]})
    if missing:
        raise ValidationFailed(
            "Some merge fields have no data source",
            code="merge_source_missing",
            details={"namespaces": missing},
        )
    return ctx


def render_body(body: str, context: Mapping[str, Any]) -> str:
    """Render the Markdown body (values HTML-escaped by the sandbox). 422 on bad fields."""
    check_fields(body)
    try:
        return str(_ENV.from_string(body).render(**context))
    except (UndefinedError, SecurityError, TypeError) as exc:
        raise ValidationFailed(
            "The template uses an unknown merge field", code="unknown_merge_field", details={"fields": []}
        ) from exc
