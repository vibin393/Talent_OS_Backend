# ruff: file-ignore[line-too-long]
"""``metering.reconcile`` (daily, ``maintenance``; NFR-71): yesterday's metered ``api_request`` count
(``usage_counters``) against the ``api_request_log`` rows, per tenant; drift above 0.1 % logs an alert."""

import dataclasses
import datetime as dt
import uuid

from loguru import logger
from sqlalchemy import func, select

from app.core.database import platform_session, tenant_session
from app.models.identity import Tenant
from app.models.metering import ApiRequestLog, UsageCounter

DRIFT_THRESHOLD = 0.001


@dataclasses.dataclass(frozen=True, slots=True)
class Drift:
    tenant_id: uuid.UUID
    day: dt.date
    metered: int
    logged: int

    @property
    def delta(self) -> int:
        return self.metered - self.logged

    @property
    def ratio(self) -> float:
        base = max(self.metered, self.logged)
        return abs(self.delta) / base if base else 0.0

    @property
    def alert(self) -> bool:
        return self.ratio > DRIFT_THRESHOLD


async def reconcile_tenant(tenant_id: uuid.UUID, day: dt.date) -> Drift:
    start = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)
    async with tenant_session(tenant_id) as db:
        metered = (
            await db.execute(
                select(func.coalesce(func.sum(UsageCounter.count), 0)).where(
                    UsageCounter.day == day, UsageCounter.unit == "api_request"
                )
            )
        ).scalar_one()
        logged = (
            await db.execute(
                select(func.count())
                .select_from(ApiRequestLog)
                .where(ApiRequestLog.at >= start, ApiRequestLog.at < end)
            )
        ).scalar_one()
    return Drift(tenant_id, day, int(metered), int(logged))


async def reconcile(
    day: dt.date | None = None, tenant_ids: list[uuid.UUID] | None = None
) -> dict[str, Drift]:
    """Per-tenant drift for ``day`` (default: yesterday UTC). Tenants with neither metered nor logged requests are
    omitted. Every drift above 0.1 % is logged as a warning (``metering_drift`` alert)."""
    day = day or (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1))
    if tenant_ids is None:
        async with platform_session() as pdb:
            tenant_ids = list((await pdb.execute(select(Tenant.id))).scalars())
    out: dict[str, Drift] = {}
    for tid in tenant_ids:
        try:
            drift = await reconcile_tenant(tid, day)
        except Exception:
            logger.exception("Metering reconcile failed for tenant {}", tid)
            continue
        if not drift.metered and not drift.logged:
            continue
        out[str(tid)] = drift
        if drift.alert:
            logger.warning(
                "metering_drift: tenant {} day {} metered={} logged={} drift={:.4%}",
                tid,
                day,
                drift.metered,
                drift.logged,
                drift.ratio,
            )
    return out


__all__ = ["DRIFT_THRESHOLD", "Drift", "reconcile", "reconcile_tenant"]
