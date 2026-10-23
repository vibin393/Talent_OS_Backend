"""Shared plumbing of the offers service: clock, permissions, principal / actor helpers, visibility of offers
(scoped through the demand, AD-11), the settings row, tenant / people lookups and presentation helpers
(money per AD-12, dates in the tenant timezone per AD-14)."""

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.permissions import Principal, can_view_compensation, check, forbidden
from app.models.demands import Demand
from app.models.offers import Offer, OfferSettings

ActorKind = Literal["user", "candidate", "system", "workflow", "ai", "api_key", "agent", "app"]  # +agent (53), +app (66)

READ = "offer:read"
WRITE = "offer:write"
RELEASE = "offer:release"
WITHDRAW = "offer:withdraw"
TEMPLATE_MANAGE = "offer_template:manage"

SUBJECT_TYPE = "offer"
LINK_PURPOSE = "offer_response"
OFFER_SEQUENCE_KEY = "OFR"
OPEN_FOR_RESPONSE = ("released", "viewed")
TERMINAL = ("accepted", "declined", "expired", "withdrawn")


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def actor_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


def actor_kind_of(ctx: RequestContext) -> ActorKind:
    """Actor of the caller: an explicit automated actor on the context wins (Epic 10 workflow)."""
    explicit = getattr(ctx, "actor_kind", None)
    if explicit in ("workflow", "ai", "agent", "system", "candidate"):
        return explicit
    kind = getattr(ctx.principal, "type", None)
    if kind == "user":
        return "user"
    if kind == "api_key":
        return "api_key"
    if kind == "app":  # Epic 66 (AD-75)
        return "app"
    if kind == "candidate":
        return "candidate"
    return "system"


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "offers service needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def can_view_money(ctx: RequestContext) -> bool:
    principal = ctx.principal if isinstance(ctx.principal, Principal) else None
    return can_view_compensation(principal)


@contextlib.contextmanager
def acting_as(db: AsyncSession, actor_type: str, actor: uuid.UUID | None = None) -> Iterator[None]:
    """Attribute audit rows flushed inside the block to ``actor_type`` (the caller flushes inside)."""
    info = db.sync_session.info
    previous = info.get(audit.ACTOR_INFO_KEY)
    audit.set_actor(db, actor_type, actor)
    try:
        yield
    finally:
        if previous is None:
            info.pop(audit.ACTOR_INFO_KEY, None)
        else:
            info[audit.ACTOR_INFO_KEY] = previous


# ================================================================ settings row
async def settings_row(db: AsyncSession) -> OfferSettings:
    row = (await db.execute(select(OfferSettings))).scalar_one_or_none()
    if row is None:
        row = OfferSettings(tenant_id=tenant_of(db))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


# ================================================================ visibility (through the demand)
def _demand_condition(principal: Principal, permission: str) -> Any:
    from app.services.demands.common import access_condition

    return access_condition(principal, permission)


def visible_offers(principal: Principal, permission: str = READ) -> Any:
    """``select(Offer)`` narrowed to offers whose demand is in ``permission``'s scope."""
    stmt = select(Offer)
    if principal.type not in ("user", "app"):  # Epic 67: delegated App Principals are scoped like users
        return stmt
    if permission not in principal.permissions:
        return stmt.where(Offer.id.is_(None))
    cond = _demand_condition(principal, permission)
    if cond is None:
        return stmt
    return stmt.where(Offer.demand_id.in_(select(Demand.id).where(cond)))


async def offer_for(
    ctx: RequestContext,
    db: AsyncSession,
    offer_id: uuid.UUID,
    permission: str = READ,
    *,
    for_update: bool = False,
) -> Offer:
    """Readable offer (else 404) on which the caller holds ``permission`` (else 403)."""
    principal = principal_of(ctx)
    if principal.type != "user":
        raise NotFound("Offer not found")
    if READ not in principal.permissions:
        raise NotFound("Offer not found")
    stmt = visible_offers(principal, READ).where(Offer.id == offer_id)
    if for_update:
        stmt = stmt.with_for_update(of=Offer)
    offer = (await db.execute(stmt)).scalar_one_or_none()
    if offer is None:
        raise NotFound("Offer not found")
    if permission != READ:
        check(principal, permission)
        scoped = visible_offers(principal, permission).where(Offer.id == offer_id)
        if (await db.execute(scoped.with_only_columns(Offer.id))).first() is None:
            raise forbidden(permission)
    return offer


async def demand_in_scope(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, permission: str
) -> Demand:
    """Return the demand when the caller may see its offers (else 404) and holds ``permission`` on it (else
    403)."""
    principal = principal_of(ctx)
    if READ not in principal.permissions:
        raise NotFound("Demand not found")
    stmt = select(Demand).where(Demand.id == demand_id)
    cond = _demand_condition(principal, READ)
    if cond is not None:
        stmt = stmt.where(cond)
    demand = (await db.execute(stmt)).scalar_one_or_none()
    if demand is None:
        raise NotFound("Demand not found")
    if permission != READ:
        check(principal, permission)
        scoped = _demand_condition(principal, permission)
        if scoped is not None:
            hit = await db.execute(select(Demand.id).where(Demand.id == demand_id, scoped))
            if hit.first() is None:
                raise forbidden(permission)
    return demand


# ================================================================ people / tenant lookups
_users = table("users", column("id"), column("name"), column("email"), column("is_active"))


async def user_contact(db: AsyncSession, user_id: uuid.UUID | None) -> tuple[str | None, str | None]:
    """``(name, email)`` of an active user."""
    if user_id is None:
        return None, None
    row = (
        await db.execute(
            select(_users.c.name, _users.c.email).where(_users.c.id == user_id, _users.c.is_active.is_(True))
        )
    ).first()
    return (str(row[0]), str(row[1])) if row else (None, None)


async def tenant_facts(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    from app.services import identity_bridge

    tenant = await identity_bridge.get_tenant(db, tenant_id)
    if tenant is None:
        return {"name": "Talent OS", "timezone": "UTC", "locale": "en-IN"}
    return {"name": tenant.name, "timezone": tenant.timezone or "UTC", "locale": tenant.locale or "en-IN"}


async def candidate_facts(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    from app.services.candidates import pipeline_port as candidates

    info = (await candidates.summaries(db, [candidate_id])).get(candidate_id, {})
    return {
        "full_name": info.get("full_name"),
        "first_name": info.get("first_name"),
        "email": await candidates.primary_email(db, candidate_id),
    }


async def client_name(db: AsyncSession, client_id: uuid.UUID) -> str | None:
    try:
        from app.services import clients_service

        return await clients_service.client_name(db, client_id)
    except Exception:  # pragma: no cover - defensive: display only
        logger.exception("client name lookup failed")
        return None


# ================================================================ time (AD-14)
def zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def tenant_today(tz_name: str | None, at: datetime | None = None) -> date:
    return (at or now()).astimezone(zone(tz_name)).date()


def end_of_day_utc(day: date, tz_name: str | None) -> datetime:
    """Return the end of ``day`` in the tenant timezone (i.e. local midnight after it) as an aware UTC
    datetime."""
    local = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone(tz_name))
    return local.astimezone(UTC)


def fmt_date(day: date | None) -> str:
    return day.strftime("%d %b %Y") if day else ""


# ================================================================ money (AD-12)
CENT = Decimal("0.01")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money(amount: Decimal | None, currency: str | None) -> dict[str, str] | None:
    if amount is None or not currency:
        return None
    return {"amount": f"{q2(Decimal(amount)):.2f}", "currency": currency}


def _indian_grouping(integer: str) -> str:
    if len(integer) <= 3:
        return integer
    head, tail = integer[:-3], integer[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def fmt_money(amount: Decimal | None, currency: str | None) -> str:
    """``INR 1,20,000.00`` (Indian grouping for INR), ``USD 120,000.00`` otherwise."""
    if amount is None or not currency:
        return ""
    value = q2(Decimal(amount))
    sign = "-" if value < 0 else ""
    integer, _, frac = f"{abs(value):.2f}".partition(".")
    grouped = _indian_grouping(integer) if currency == "INR" else f"{int(integer):,}"
    return f"{currency} {sign}{grouped}.{frac}"


UNIT_LABELS = {"hour": "per hour", "day": "per day", "month": "per month", "year": "per year"}
EMPLOYMENT_LABELS = {
    "contract": "Contract",
    "contract_to_hire": "Contract to hire",
    "permanent": "Permanent",
    "freelance": "Freelance",
}
WORK_MODE_LABELS = {"onsite": "On-site", "hybrid": "Hybrid", "remote": "Remote"}
