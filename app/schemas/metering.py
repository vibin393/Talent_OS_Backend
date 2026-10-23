# ruff: file-ignore[line-too-long]
"""Usage metering reads (Epic 66, AD-78, FR-338 / FR-385). Informational only (D-12): no billing objects."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.metering import USAGE_UNITS

UsageGroup = Literal["day", "unit", "installation", "api_key", "route_group"]
MAX_RANGE_DAYS = 400


class _Range(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date
    units: list[str] | None = None

    @model_validator(mode="after")
    def _check(self) -> "_Range":
        if self.date_to < self.date_from:
            msg = "date_to must not be before date_from"
            raise ValueError(msg)
        if (self.date_to - self.date_from).days > MAX_RANGE_DAYS:
            msg = f"the range may span at most {MAX_RANGE_DAYS} days"
            raise ValueError(msg)
        for unit in self.units or ():
            if unit not in USAGE_UNITS:
                msg = f"unknown unit {unit!r}"
                raise ValueError(msg)
        return self


class UsageQuery(_Range):
    """Tenant usage between two UTC days (inclusive), grouped by ``group_by``."""

    installation_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    group_by: list[UsageGroup] = Field(default_factory=lambda: ["day", "unit"], min_length=1)


class UsageRow(BaseModel):
    day: date | None = None
    unit: str | None = None
    installation_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    route_group: str | None = None
    count: int = 0
    error_count: int = 0
    quantity: Decimal = Decimal(0)


class UnitTotal(BaseModel):
    unit: str
    count: int = 0
    error_count: int = 0
    quantity: Decimal = Decimal(0)


class UsageOut(BaseModel):
    date_from: date
    date_to: date
    group_by: list[UsageGroup]
    rows: list[UsageRow]
    totals: list[UnitTotal]


class PlatformUsageQuery(_Range):
    """Operator view: usage by App across tenants (FR-385: no tenant names). Sandbox Tenants excluded by default."""

    app_id: uuid.UUID | None = None
    include_sandbox: bool = False


class PlatformUsageRow(BaseModel):
    app_id: uuid.UUID | None  # None = calls not made by an App (API keys, platform connectors)
    unit: str
    count: int = 0
    error_count: int = 0
    quantity: Decimal = Decimal(0)
    tenant_count: int = 0


class PlatformUsageOut(BaseModel):
    date_from: date
    date_to: date
    rows: list[PlatformUsageRow]


__all__ = [
    "PlatformUsageOut",
    "PlatformUsageQuery",
    "PlatformUsageRow",
    "UnitTotal",
    "UsageGroup",
    "UsageOut",
    "UsageQuery",
    "UsageRow",
]
