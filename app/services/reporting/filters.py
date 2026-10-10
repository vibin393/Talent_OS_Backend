"""Dashboard filters (Epic 11, A2): date range in the user's timezone, business units, granularity.

* ``from`` / ``to`` are local dates (users carry no timezone yet: the tenant's) converted to the UTC
  half-open range ``[from 00:00, to+1 00:00)``. Default: the last 30 days (today included).
  A range longer than ``DASHBOARD_MAX_RANGE_DAYS`` or ``from > to`` -> 422 ``invalid_range``.
* ``business_unit_ids`` must be a subset of the BUs the principal sees under ``report:read`` (tenant scope:
  every BU of the tenant; ``bu`` scope: the principal's BU) else 403 ``bu_out_of_scope``. Empty = every BU
  in scope. Principals with ``own`` / ``team`` report scope ignore the filter ("my" numbers).
* ``granularity`` of series tiles: ``day | week | month``, auto-picked from the range length.
"""

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, ValidationFailed
from app.core.permissions import Principal, Scope
from app.core.settings import get_settings
from app.services.reporting.common import business_units, day_range, now

Granularity = Literal["day", "week", "month"]
GRANULARITIES: tuple[str, ...] = ("day", "week", "month")
REPORT_READ = "report:read"


@dataclasses.dataclass(frozen=True, slots=True)
class DashboardFilters:
    from_date: date
    to_date: date
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive
    tz: ZoneInfo
    business_unit_ids: tuple[uuid.UUID, ...]
    granularity: Granularity
    bu_filter_ignored: bool = False
    # Epic 34 (A1): Legal Entities (empty = all) and the "as of" day of aging-type tiles (default ``to``).
    legal_entity_ids: tuple[uuid.UUID, ...] = ()
    as_of_date: date | None = None
    # Epic 65: the ``?agent_key=`` filter of the Phase 4 agent tiles (None = every agent).
    agent_key: str | None = None

    @property
    def as_of(self) -> date:
        return self.as_of_date or self.to_date

    def le_query(self) -> dict[str, list[str]]:
        return {"legal_entity_ids": [str(x) for x in self.legal_entity_ids]} if self.legal_entity_ids else {}

    @property
    def tz_name(self) -> str:
        return str(self.tz.key)

    def cache_key(self) -> str:
        material = json.dumps(
            {
                "from": self.from_date.isoformat(),
                "to": self.to_date.isoformat(),
                "tz": self.tz_name,
                "bu": sorted(str(b) for b in self.business_unit_ids),
                "g": self.granularity,
                "le": sorted(str(x) for x in self.legal_entity_ids),
                "asof": self.as_of.isoformat(),
                "agent": self.agent_key,
            },
            sort_keys=True,
        )
        return hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()

    def range_query(self) -> dict[str, str]:
        return {"from": self.from_date.isoformat(), "to": self.to_date.isoformat()}

    def bu_query(self) -> dict[str, list[str]]:
        return (
            {"business_unit_ids": [str(b) for b in self.business_unit_ids]} if self.business_unit_ids else {}
        )


def auto_granularity(days: int) -> Granularity:
    if days <= 31:
        return "day"
    if days <= 183:
        return "week"
    return "month"


def invalid_range(message: str, **details: object) -> ValidationFailed:
    return ValidationFailed(message, code="invalid_range", details=details or None)


async def in_scope_business_units(db: AsyncSession, principal: Principal) -> set[uuid.UUID] | None:
    """BUs the principal may filter on under ``report:read``; None = the filter does not apply (own/team)."""
    scope = principal.scope_of(REPORT_READ)
    if scope is None or not scope.covers(Scope.BU):
        return None
    if scope is Scope.TENANT:
        return set((await db.execute(select(business_units.c.id))).scalars())
    return {principal.business_unit_id} if principal.business_unit_id is not None else set()


async def resolve(
    db: AsyncSession,
    principal: Principal,
    tz: ZoneInfo,
    from_date: date | None,
    to_date: date | None,
    business_unit_ids: Sequence[uuid.UUID] | None,
    granularity: str | None,
    *,
    reference: datetime | None = None,
    legal_entity_ids: Sequence[uuid.UUID] | None = None,
    as_of: date | None = None,
    agent_key: str | None = None,
) -> DashboardFilters:
    today = (reference or now()).astimezone(tz).date()
    to_d = to_date or (today if from_date is None else max(from_date, today))
    from_d = from_date or (to_d - timedelta(days=29))
    if from_d > to_d:
        raise invalid_range(
            "`from` must not be after `to`", **{"from": from_d.isoformat(), "to": to_d.isoformat()}
        )
    days = (to_d - from_d).days + 1
    max_days = get_settings().dashboard_max_range_days
    if days > max_days:
        raise invalid_range(f"The date range may span at most {max_days} days", days=days, max_days=max_days)
    if granularity is not None and granularity not in GRANULARITIES:
        raise ValidationFailed(
            "Unknown granularity", code="validation_failed", details={"granularity": granularity}
        )
    wanted = tuple(dict.fromkeys(business_unit_ids or ()))
    allowed = await in_scope_business_units(db, principal)
    ignored = False
    if allowed is None:
        ignored = bool(wanted)
        wanted = ()
    elif not set(wanted) <= allowed:
        raise Forbidden(
            "One or more business units are outside your reporting scope",
            code="bu_out_of_scope",
            details={"business_unit_ids": sorted(str(b) for b in set(wanted) - allowed)},
        )
    start, end = day_range(from_d, to_d, tz)
    return DashboardFilters(
        from_date=from_d,
        to_date=to_d,
        start=start,
        end=end,
        tz=tz,
        business_unit_ids=wanted,
        granularity=granularity or auto_granularity(days),  # type: ignore[arg-type]
        bu_filter_ignored=ignored,
        legal_entity_ids=await resolve_legal_entities(db, legal_entity_ids),
        as_of_date=as_of,
        agent_key=agent_key or None,
    )


# ================================================================ Epic 34 (Phase 2) additions
# Spec permission keys -> catalogue keys (RECONCILED CONTRACT).
PERMISSION_MAP: dict[str, str] = {
    "invoice:read": "billing:read",
    "payment:read": "receivables:read",
    "collections:read": "receivables:read",
    "credential:read": "compliance:read",
    "executive:read": "executive_dashboard:read",
}
FINANCE_EXPORT = "report:finance_export"
MAX_EXEC_MONTHS = 24


# Until Epic 30 adds receivables:read to the catalogue, receivables reads fall back to billing:read.
PERMISSION_FALLBACK: dict[str, str] = {"receivables:read": "billing:read"}


def perm(key: str) -> str:
    """Return the catalogue key of a spec permission key (with the pre-merge fallback)."""
    from app.core.permissions import CATALOG

    mapped = PERMISSION_MAP.get(key, key)
    if mapped not in CATALOG and mapped in PERMISSION_FALLBACK:
        return PERMISSION_FALLBACK[mapped]
    return mapped


def holds(principal: Principal, key: str) -> bool:
    return principal.has(perm(key))


async def resolve_legal_entities(db: AsyncSession, ids: Sequence[uuid.UUID] | None) -> tuple[uuid.UUID, ...]:
    """Validate ``legal_entity_ids`` (RLS hides other tenants' rows) -> 422 ``unknown_legal_entity``."""
    wanted = tuple(dict.fromkeys(ids or ()))
    if not wanted:
        return ()
    from app.services.reporting.p2tables import legal_entities

    try:
        stmt = select(legal_entities.c.id).where(legal_entities.c.id.in_(wanted))
        found = set((await db.execute(stmt)).scalars())
    except Exception as exc:  # pragma: no cover - foundation table always present in Phase 2
        raise ValidationFailed("Unknown legal entity", code="unknown_legal_entity") from exc
    missing = [str(x) for x in wanted if x not in found]
    if missing:
        raise ValidationFailed(
            "Unknown legal entity", code="unknown_legal_entity", details={"legal_entity_ids": missing}
        )
    return wanted


def month_range(month_from: date | None, month_to: date | None, today: date) -> tuple[date, date]:
    """Return the executive month range (first days); default the last 12 months; at most 24 -> 422."""
    last = (month_to or today).replace(day=1)
    if month_from is not None:
        first = month_from.replace(day=1)
    else:
        y, m = divmod(last.year * 12 + last.month - 1 - 11, 12)
        first = date(y, m + 1, 1)
    if first > last:
        raise invalid_range("`month_from` must not be after `month_to`")
    months = (last.year - first.year) * 12 + last.month - first.month + 1
    if months > MAX_EXEC_MONTHS:
        raise invalid_range(f"The month range may span at most {MAX_EXEC_MONTHS} months", months=months)
    return first, last
