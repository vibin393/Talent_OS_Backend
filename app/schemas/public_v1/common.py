# ruff: noqa: E501
"""Public API v1 shared shapes (Epic 67, spec A4): the frozen contract. Outputs list explicit fields; inputs forbid
unknown fields. Enums are documented as open (``x-extensible-enum``): values may be added within v1."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def open_enum(*values: str) -> dict[str, Any]:
    """``json_schema_extra`` for an open string enum (documented values; more may be added in v1)."""
    return {"x-extensible-enum": list(values)}


class Money(BaseModel):
    amount: str = Field(description="Decimal string, e.g. 1234.50")
    currency: str


class PublicObject(BaseModel):
    id: uuid.UUID
    object: str
    human_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: str = Field(description="Send as If-Match (W/\"<version>\") on PATCH")
    masked_fields: list[str] = Field(default_factory=list)


class PublicIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page[T](BaseModel):
    data: list[T]
    next_cursor: str | None = None
    has_more: bool = False
