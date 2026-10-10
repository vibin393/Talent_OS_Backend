"""Shared plumbing of the reporting service (Epic 11).

**AD-1 read-model exception.** Reporting is the only domain that reads other domains' tables directly, and
only with read-only aggregate / list SQL (no writes, ever). Existing domains are read through their ORM
classes; tables of epics that may be absent in a deployment (interviews, offers) through lightweight
``sqlalchemy.table()`` projections guarded by ``available()`` (checked once per process with ``to_regclass``
and ``information_schema``), so a missing table hides its tiles instead of failing.

Row visibility always goes through the owning domain's scope rules: demand-bound rows use
``demands.access_condition(principal, <kind read permission>)`` (scope_filter on owner / BU plus assignment),
exactly like the list endpoints. RLS confines every statement to the session's tenant.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    ColumnElement,
    DateTime,
    Integer,
    Numeric,
    Select,
    String,
    Uuid,
    column,
    false,
    select,
    table,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal
from app.models.demands import Demand, DemandAssignment

# ================================================================ optional tables (Epics 7-8)
interviews = table(
    "interviews",
    column("id", Uuid()),
    column("human_id", String()),
    column("application_id", Uuid()),
    column("demand_id", Uuid()),
    column("candidate_id", Uuid()),
    column("stage_name", String()),
    column("round", Integer()),
    column("starts_at", DateTime(timezone=True)),
    column("ends_at", DateTime(timezone=True)),
    column("mode", String()),
    column("status", String()),
    column("organizer_user_id", Uuid()),
    column("created_at", DateTime(timezone=True)),
)
interview_participants = table(
    "interview_participants",
    column("interview_id", Uuid()),
    column("user_id", Uuid()),
    column("removed_at", DateTime(timezone=True)),
)
offers = table(
    "offers",
    column("id", Uuid()),
    column("human_id", String()),
    column("application_id", Uuid()),
    column("demand_id", Uuid()),
    column("candidate_id", Uuid()),
    column("client_id", Uuid()),
    column("status", String()),
    column("owner_id", Uuid()),
    column("current_version_id", Uuid()),
    column("released_version_id", Uuid()),
    column("released_at", DateTime(timezone=True)),
    column("viewed_at", DateTime(timezone=True)),
    column("responded_at", DateTime(timezone=True)),
    column("expires_at", DateTime(timezone=True)),
    column("created_at", DateTime(timezone=True)),
)
offer_versions = table(
    "offer_versions",
    column("id", Uuid()),
    column("version_no", Integer()),
    column("title", String()),
    column("employment_type", String()),
    column("pay_rate", Numeric()),
    column("pay_currency", String()),
    column("pay_rate_unit", String()),
    column("bill_rate", Numeric()),
    column("bill_currency", String()),
    column("margin_amount", Numeric()),
    column("margin_pct", Numeric()),
    column("start_date", DateTime()),
)

# Identity / directory projections (platform + tenant tables of the identity module).
users = table(
    "users",
    column("id", Uuid()),
    column("name", String()),
    column("is_active", Boolean()),
    column("business_unit_id", Uuid()),
)
roles = table("roles", column("id", Uuid()), column("key", String()))
user_roles = table("user_roles", column("user_id", Uuid()), column("role_id", Uuid()))
business_units = table("business_units", column("id", Uuid()), column("name", String()))

# Columns each optional table must have for the reporting SQL above.
OPTIONAL_TABLES: dict[str, tuple[str, ...]] = {
    "interviews": (
        "id",
        "application_id",
        "demand_id",
        "candidate_id",
        "starts_at",
        "status",
        "organizer_user_id",
    ),
    "interview_participants": ("interview_id", "user_id"),
    "offers": (
        "id",
        "application_id",
        "demand_id",
        "status",
        "released_at",
        "responded_at",
        "released_version_id",
    ),
    "offer_versions": (
        "id",
        "pay_rate",
        "pay_currency",
        "bill_rate",
        "bill_currency",
        "margin_amount",
        "margin_pct",
    ),
}
_available: frozenset[str] | None = None


async def available(db: AsyncSession) -> frozenset[str]:
    """Return the optional tables present with the expected columns (checked once per process)."""
    global _available  # process-wide cache: schema only changes with a deploy
    if _available is None:
        rows = await db.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ANY(:names)"
            ),
            {"names": list(OPTIONAL_TABLES)},
        )
        present: dict[str, set[str]] = {}
        for table_name, column_name in rows.tuples():
            present.setdefault(str(table_name), set()).add(str(column_name))
        _available = frozenset(
            name for name, needed in OPTIONAL_TABLES.items() if set(needed) <= present.get(name, set())
        )
    return _available


def reset_availability() -> None:
    """Test hook: re-check the optional tables on next use."""
    global _available
    _available = None


def has_interviews(avail: frozenset[str]) -> bool:
    return {"interviews", "interview_participants"} <= avail


def has_offers(avail: frozenset[str]) -> bool:
    return {"offers", "offer_versions"} <= avail


# ================================================================ principal / scope
def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "reporting needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def demand_scope(principal: Principal, permission: str) -> ColumnElement[bool] | None:
    """Demands on which ``principal`` holds ``permission`` (the demands domain's own visibility rule)."""
    from app.services import demands as demands_service

    if permission not in principal.permissions:
        return false()
    return demands_service.access_condition(principal, permission)


def scoped_demand_ids(
    principal: Principal,
    permission: str,
    business_unit_ids: Iterable[uuid.UUID] = (),
    client_owner: uuid.UUID | None = None,
) -> Select[Any]:
    """``SELECT demands.id`` visible under ``permission``, optionally narrowed to BUs / owned clients."""
    from app.models.clients import Client

    stmt = select(Demand.id)
    cond = demand_scope(principal, permission)
    if cond is not None:
        stmt = stmt.where(cond)
    bus = list(business_unit_ids)
    if bus:
        stmt = stmt.where(Demand.business_unit_id.in_(bus))
    if client_owner is not None:
        stmt = stmt.where(Demand.client_id.in_(select(Client.id).where(Client.owner_id == client_owner)))
    return stmt


def my_demand_ids(principal: Principal) -> Select[Any]:
    """Demands the principal is an active assignee of, directly or through a team (the demands list's
    ``assignee=me`` rule)."""
    from sqlalchemy import or_

    who = [DemandAssignment.user_id == principal.id]
    if principal.team_ids:
        who.append(DemandAssignment.team_id.in_(principal.team_ids))
    return select(DemandAssignment.demand_id).where(DemandAssignment.removed_at.is_(None), or_(*who))


# ================================================================ time
def now() -> datetime:
    return datetime.now(UTC)


async def tenant_tz(db: AsyncSession) -> ZoneInfo:
    from app.services import entity_refs

    return (await entity_refs.tenant(db, tenant_of(db))).tz


def local_start(day: date, tz: ZoneInfo) -> datetime:
    """UTC instant of local midnight starting ``day``."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def day_range(first: date, last: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Half-open UTC range ``[first 00:00, last+1 00:00)`` in ``tz``."""
    return local_start(first, tz), local_start(last + timedelta(days=1), tz)


def business_days_ago(reference: datetime, days: int, tz: ZoneInfo) -> datetime:
    """Return the instant ``days`` business days (Mon-Fri) before ``reference``, in local time."""
    local = reference.astimezone(tz)
    remaining = days
    while remaining > 0:
        local -= timedelta(days=1)
        if local.weekday() < 5:
            remaining -= 1
    return local.astimezone(UTC)


def iso_week(reference: datetime, tz: ZoneInfo) -> tuple[date, date]:
    """Monday..Sunday of the local ISO week containing ``reference``."""
    today = reference.astimezone(tz).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


async def role_keys(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(roles.c.key)
        .join(user_roles, user_roles.c.role_id == roles.c.id)
        .where(user_roles.c.user_id == user_id)
    )
    return {str(k) for k in rows.scalars()}


async def role_user_ids(db: AsyncSession, role_key: str) -> list[uuid.UUID]:
    """Active users holding the role ``role_key`` (Epic 34: Finance Admin alert recipients)."""
    rows = await db.execute(
        select(user_roles.c.user_id)
        .join(roles, roles.c.id == user_roles.c.role_id)
        .join(users, users.c.id == user_roles.c.user_id)
        .where(roles.c.key == role_key, users.c.is_active.is_(True))
    )
    return list(dict.fromkeys(rows.scalars()))
