# ruff: noqa: E501
"""Shared helpers of the ``compliance`` domain (Epic 21): permissions, settings, tenant clock, local events."""

import dataclasses
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.fiscal import today_for
from app.core.permissions import Principal, forbidden
from app.models.compliance import ComplianceSettings

COMPLIANCE_READ = "compliance:read"
COMPLIANCE_CONFIGURE = "compliance:configure"
COMPLIANCE_WAIVE = "compliance:waive"
CREDENTIAL_WRITE = "credential:write"
CREDENTIAL_VERIFY = "credential:verify"
IDENTITY_VIEW = "identity:view"

OPEN_PLACEMENT_STATUSES = ("pre_joining", "joining_confirmed", "active", "offboarding")
REEVALUATE_STATUSES = ("pre_joining", "joining_confirmed", "active")
DEFAULT_OFFSETS = (60, 30, 7)


def now() -> datetime:
    return datetime.now(UTC)


def not_found(what: str = "Resource") -> NotFound:
    return NotFound(f"{what} not found")


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:  # pragma: no cover - tenant sessions only
        msg = "a tenant session is required"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def user_principal(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    if isinstance(p, Principal) and p.type == "user":
        return p
    return None


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    p = user_principal(ctx)
    return p.id if p else None


def has(ctx: RequestContext, permission: str) -> bool:
    p = user_principal(ctx)
    return p is not None and p.scope_of(permission) is not None


def require_human(ctx: RequestContext, permission: str) -> uuid.UUID:
    """Waive and verify are decided by human users only (never workflow / system / AI actors)."""
    uid = user_id(ctx)
    if uid is None or ctx.actor_kind not in (None, "user") or not has(ctx, permission):
        raise forbidden(permission)
    return uid


def system_ctx(db: AsyncSession) -> RequestContext:
    return RequestContext(tenant_id=tenant_of(db), actor_kind="system")


async def get_settings_row(db: AsyncSession) -> ComplianceSettings:
    row = (await db.execute(select(ComplianceSettings))).scalar_one_or_none()
    if row is not None:
        return row
    try:
        async with db.begin_nested():
            await db.execute(
                pg_insert(ComplianceSettings)
                .values(id=uuid.uuid4(), tenant_id=tenant_of(db))
                .on_conflict_do_nothing(constraint="uq_compliance_settings_tenant_id")
            )
    except Exception:  # a worker session cannot write configuration: use the defaults
        return ComplianceSettings(
            evaluate_at="placement_created",
            default_alert_offsets=list(DEFAULT_OFFSETS),
            recompute_hour_local=2,
            timezone="Asia/Kolkata",
        )
    return (await db.execute(select(ComplianceSettings))).scalar_one()


async def today(db: AsyncSession, at: datetime | None = None) -> date:
    s = await get_settings_row(db)
    return today_for(s.timezone or "Asia/Kolkata", at)


# ---------------------------------------------------------------- local (after-commit) events
@dataclasses.dataclass(frozen=True, slots=True)
class ReevaluationRequested:
    tenant_id: uuid.UUID
    requirement_id: uuid.UUID
    run_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CredentialExpiring:
    tenant_id: uuid.UUID
    credential_id: uuid.UUID
    candidate_id: uuid.UUID
    type_key: str
    offset_days: int
    expires_on: date | None


@dataclasses.dataclass(frozen=True, slots=True)
class CredentialExpired:
    tenant_id: uuid.UUID
    credential_id: uuid.UUID
    candidate_id: uuid.UUID
    type_key: str
    expires_on: date | None


@dataclasses.dataclass(frozen=True, slots=True)
class RedactionRejected:
    tenant_id: uuid.UUID
    credential_id: uuid.UUID
    candidate_id: uuid.UUID
    type_key: str
