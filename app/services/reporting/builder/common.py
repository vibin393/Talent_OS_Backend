# ruff: file-ignore[non-imperative-mood]
"""Shared helpers of the report builder (Epic 47): principal, clock, JSON-safe values, permission keys."""

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.core.permissions import Principal

__all__ = [
    "BUILD",
    "EXPORT",
    "FINANCE_EXPORT",
    "READ",
    "SCHEDULE",
    "SHARE_TENANT",
    "jsonable",
    "now",
    "principal_of",
]

READ = "report:read"
EXPORT = "report:export"
FINANCE_EXPORT = "report:finance_export"
BUILD = "report:build"
SHARE_TENANT = "report:share_tenant"
SCHEDULE = "report:schedule"


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise Forbidden("Custom reports are available to internal users only")
    return principal


def jsonable(value: Any) -> Any:
    """Result cell -> JSON-safe value (money stays exact: Decimal -> str)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return value
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return str(value)
