# ruff: file-ignore[line-too-long]
"""The compact semantic catalogue the model sees (Epic 64 story 64.1, ``semantic.catalogue``).

Built from ``core.semantic.catalogue_for`` / ``source_catalogue`` for the caller's principal (the Effective Rights): a
source is listed only with its read permission, a field only with its field / join permission. Masked fields
(``compensation`` / ``identity`` without the view permission) are listed with ``masked=true`` and no operators,
aggregates or grouping, so the model knows they exist but cannot filter, group or measure by them. Enum values are
capped at 50. ``labels_for`` feeds ``describe`` (field labels, types, enum keys).
"""

from app.core.permissions import Principal
from app.core.semantic import catalogue_for, get_source, source_catalogue
from app.schemas.analytics_answers import CatalogueFieldOut, CatalogueOut, CatalogueSourceOut
from app.services.analytics_answers.describe import Labels

__all__ = ["MAX_ENUM_VALUES", "compact", "labels_for", "source_label"]

MAX_ENUM_VALUES = 50


def compact(principal: Principal, source_key: str | None) -> CatalogueOut:
    if source_key is None:
        return CatalogueOut(
            sources=[
                CatalogueSourceOut(key=s.key, label=s.label, field_count=len(s.fields))
                for s in catalogue_for(principal)
            ]
        )
    src = source_catalogue(principal, source_key)  # 404 semantic_unknown_source when unknown / not readable
    joins = sorted(get_source(source_key).joins)
    fields = [
        CatalogueFieldOut(
            key=f.key,
            label=f.label,
            type=f.type,
            ops=list(f.ops),
            aggs=list(f.aggs),
            groupable=f.groupable,
            date_buckets=f.date_buckets,
            enum_values=list(f.enum_values[:MAX_ENUM_VALUES]) if f.enum_values else None,
            masked=f.masked,
        )
        for f in src.fields
    ]
    return CatalogueOut(key=src.key, label=src.label, fields=fields, joins=joins)


def source_label(source_key: str) -> str | None:
    try:
        return get_source(source_key).label
    except KeyError:
        return None


def labels_for(source_key: str) -> Labels:
    """Registry labels (not principal-filtered: an explanation names the keys the definition already uses)."""
    try:
        src = get_source(source_key)
    except KeyError:
        return Labels(source_label=source_key)
    return Labels(
        source_label=src.label,
        fields={k: f.label for k, f in src.fields.items()},
        types={k: f.type for k, f in src.fields.items()},
        enums=frozenset(k for k, f in src.fields.items() if f.type == "enum"),
    )
