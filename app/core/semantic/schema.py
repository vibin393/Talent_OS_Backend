"""Semantic definition DTOs (Epic 36 contract 0.6; pydantic, ``extra="forbid"``).

A filter node is a ``Condition`` (``{"field", "op", "value"}``), an ``AndNode`` (``{"and": [...]}``) or an
``OrNode`` (``{"or": [...]}``). Limits (depth <= 5, <= 50 conditions, list sizes) are enforced by
``compiler.validate`` so they report ``semantic_too_complex`` with a JSON path.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from app.core.semantic.types import Op

__all__ = [
    "MAX_CONDITIONS",
    "MAX_DEPTH",
    "MAX_FIELDS",
    "MAX_GROUP_BY",
    "MAX_MEASURES",
    "MAX_SORT",
    "AndNode",
    "Condition",
    "Definition",
    "GroupBy",
    "Measure",
    "Node",
    "OrNode",
    "SortSpec",
]

MAX_DEPTH = 5
MAX_CONDITIONS = 50
MAX_FIELDS = 50
MAX_GROUP_BY = 3
MAX_MEASURES = 10
MAX_SORT = 3

_STRICT = ConfigDict(extra="forbid", populate_by_name=True)


class Condition(BaseModel):
    model_config = _STRICT

    field: str
    op: Op
    value: Any = None


class AndNode(BaseModel):
    model_config = _STRICT

    and_: list["Node"] = Field(alias="and", min_length=1)


class OrNode(BaseModel):
    model_config = _STRICT

    or_: list["Node"] = Field(alias="or", min_length=1)


def _node_tag(value: Any) -> str:
    if isinstance(value, dict):
        if "and" in value or "and_" in value:
            return "and"
        if "or" in value or "or_" in value:
            return "or"
        return "condition"
    if isinstance(value, AndNode):
        return "and"
    if isinstance(value, OrNode):
        return "or"
    return "condition"


Node = Annotated[
    Annotated[Condition, Tag("condition")] | Annotated[AndNode, Tag("and")] | Annotated[OrNode, Tag("or")],
    Discriminator(_node_tag),
]


class GroupBy(BaseModel):
    model_config = _STRICT

    field: str
    bucket: Literal["day", "week", "month", "quarter", "fy"] | None = None


class Measure(BaseModel):
    model_config = _STRICT

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    agg: Literal["count", "count_distinct", "sum", "avg", "min", "max", "ratio", "pct_of_total"]
    field: str | None = None
    of: tuple[str, str] | None = None  # ratio(m1, m2) by measure keys


class SortSpec(BaseModel):
    model_config = _STRICT

    key: str
    desc: bool = False


class Definition(BaseModel):
    model_config = _STRICT

    definition_version: Literal[1] = 1
    source: str
    filters: Node | None = None
    fields: list[str] = []
    group_by: list[GroupBy] = []
    measures: list[Measure] = []
    sort: list[SortSpec] = []


AndNode.model_rebuild()
OrNode.model_rebuild()
Definition.model_rebuild()
