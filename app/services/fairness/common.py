# ruff: noqa: E501
"""Shared helpers of the ``fairness`` domain (Epic 49): permission keys, errors, clock and tenant / user access."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed

FAIRNESS_READ = "fairness:read"
FAIRNESS_CONFIGURE = "fairness:configure"
AEDT_MANAGE = "aedt:manage"
CONSENT_PURPOSE = "demographic_self_id"
CONSENT_SOURCE = "self_id_form"


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, field: str | None = None) -> ValidationFailed:
    details: dict[str, Any] = {}
    if field:
        details["fields"] = [{"field": field, "message": message}]
    return ValidationFailed(message, code=code, details=details or None)


def not_found(what: str = "Resource") -> NotFound:
    return NotFound(f"{what} not found")


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if getattr(principal, "type", None) == "user" else None
