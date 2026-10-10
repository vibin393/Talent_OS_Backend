"""What a principal may use from the semantic sources (Epic 36 contract 0.6).

A source is listed when the principal holds its read permission; a field is listed unless its own (or its
join's) ``permission`` is missing. ``compensation`` / ``identity`` fields without ``compensation:view`` /
``identity:view`` are listed ``masked`` with no operators, aggregates or grouping (display as ``null`` only).
External principals see nothing.
"""

from app.core.errors import NotFound
from app.core.permissions import Principal
from app.core.semantic.compiler import INTERNAL_PRINCIPALS, PII_PERMISSIONS
from app.core.semantic.registry import sources
from app.core.semantic.types import FieldDef, JoinDef, SourceDef
from app.schemas.semantic import FieldCatalogOut, SourceCatalogOut

__all__ = ["catalogue_for", "source_catalogue"]


def _field(key: str, fd: FieldDef, join: JoinDef | None, principal: Principal) -> FieldCatalogOut | None:
    for permission in (fd.permission, join.permission if join is not None else None):
        if permission is not None and not principal.has(permission):
            return None
    pii_permission = PII_PERMISSIONS.get(fd.pii_class)
    masked = pii_permission is not None and not principal.has(pii_permission)
    many = join is not None and join.many
    return FieldCatalogOut(
        key=key,
        label=fd.label,
        type=fd.type,
        pii_class=fd.pii_class,
        ops=[] if masked else sorted(fd.ops),
        aggs=[] if masked else sorted(fd.aggs),
        groupable=fd.groupable and not masked,
        date_buckets=fd.date_buckets and not masked,
        enum_values=list(fd.enum_values) if fd.enum_values else None,
        join=fd.join,
        displayable=not many and fd.type != "skill",
        masked=masked,
    )


def _source(src: SourceDef, principal: Principal) -> SourceCatalogOut:
    fields = []
    for key in sorted(src.fields):
        fd = src.fields[key]
        out = _field(key, fd, src.joins.get(fd.join) if fd.join else None, principal)
        if out is not None:
            fields.append(out)
    return SourceCatalogOut(key=src.key, label=src.label, fields=fields)


def _allowed(src: SourceDef, principal: Principal) -> bool:
    return principal.type in INTERNAL_PRINCIPALS and principal.has(src.scope.permission)


def catalogue_for(principal: Principal) -> list[SourceCatalogOut]:
    """List the sources (sorted by key) and the fields ``principal`` may use."""
    return [_source(src, principal) for key, src in sorted(sources().items()) if _allowed(src, principal)]


def source_catalogue(principal: Principal, key: str) -> SourceCatalogOut:
    """One source's catalogue (404 when unknown or not readable by ``principal``)."""
    src = sources().get(key)
    if src is None or not _allowed(src, principal):
        raise NotFound("Unknown semantic source", code="semantic_unknown_source")
    return _source(src, principal)
