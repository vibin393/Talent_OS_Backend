"""Semantic layer catalogue / result DTOs (Epic 36 contract 0.6).

Leaf module (pydantic only): ``app.core.semantic`` builds these. The definition DTOs (``Definition``,
``Node``, ...) live in ``app.core.semantic.schema``.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = ["ColumnMeta", "FieldCatalogOut", "SemanticErrorOut", "SourceCatalogOut", "ValidateOut"]


class ColumnMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    type: str
    masked: bool = False


class FieldCatalogOut(BaseModel):
    """One field the principal may use. ``masked``: displayable only as ``null`` (no filter / group / measure
    use); ``displayable``: may appear in ``fields`` (false for many-join and skill fields)."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    type: str
    pii_class: str
    ops: list[str]
    aggs: list[str]
    groupable: bool
    date_buckets: bool
    enum_values: list[str] | None = None
    join: str | None = None
    displayable: bool
    masked: bool = False


class SourceCatalogOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    fields: list[FieldCatalogOut]


class SemanticErrorOut(BaseModel):
    code: str
    message: str
    path: str
    details: dict[str, Any] = {}


class ValidateOut(BaseModel):
    valid: bool
    errors: list[SemanticErrorOut]
