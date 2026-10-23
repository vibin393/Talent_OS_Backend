# ruff: file-ignore[line-too-long]
"""Client Portal hiring forecasts (Epic 44, FR-226 / FR-80): ``/api/v1/client-portal/forecasts``.

Whitelisted by construction: no budget, bill rate, approval chain, owner or internal note ever appears. A Client's
own line comment is returned as ``comment`` (the internal ``notes`` column is never read here). Every response model
is registered with ``register_portal_schema("client_user")`` (walked by ``tests/portal/test_field_visibility.py``).
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.field_visibility import register_portal_schema
from app.schemas.workforce import Month

ForecastStatus = Literal["submitted", "in_review", "approved", "declined"]

__all__ = [
    "ForecastIn",
    "ForecastLineIn",
    "ForecastLinePortalOut",
    "ForecastPortalDetail",
    "ForecastPortalOut",
    "ForecastSkillIn",
    "ForecastStatus",
    "ForecastUpdate",
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForecastSkillIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ForecastLineIn(_In):
    role_title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    skills: list[ForecastSkillIn] = Field(default_factory=list, max_length=30)
    headcount: int = Field(ge=1, le=500)
    location_text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    planned_start_month: Month
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class ForecastIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    period_start: date
    period_end: date
    lines: list[ForecastLineIn] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _period(self) -> "ForecastIn":
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self


class ForecastUpdate(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    period_start: date | None = None
    period_end: date | None = None
    lines: list[ForecastLineIn] | None = Field(default=None, min_length=1, max_length=200)


@register_portal_schema("client_user")
class ForecastLinePortalOut(BaseModel):
    id: uuid.UUID
    role_title: str
    skills: list[str]
    headcount: int
    location_text: str | None
    planned_start_month: str
    comment: str | None


@register_portal_schema("client_user")
class ForecastPortalOut(BaseModel):
    id: uuid.UUID
    name: str
    period_start: date
    period_end: date
    status: ForecastStatus
    created_at: datetime
    line_count: int
    total_headcount: int
    editable: bool


@register_portal_schema("client_user")
class ForecastPortalDetail(ForecastPortalOut):
    lines: list[ForecastLinePortalOut]
