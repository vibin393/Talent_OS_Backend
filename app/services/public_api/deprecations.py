# ruff: noqa: E501
"""Monthly deprecated-call report (Epic 67, FR-347, spec A5): last month's ``api_request_log`` rows with
``deprecated = true``, per Installation or API key and route, upserted into ``api_deprecation_notices``; each new notice
emits ``PublicApiDeprecatedUsage`` (notification ``api_deprecated_usage`` to the Installation's accountable user or the
key's creator and to ``integration:manage`` holders)."""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.core.public_api_deprecations import DEPRECATIONS
from app.events.public_api import PublicApiDeprecatedUsage
from app.models.metering import ApiRequestLog
from app.models.public_api import ApiDeprecationNotice


def previous_month(today: date) -> tuple[date, date]:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), first_this


def _sunset(route: str | None) -> date:
    dep = DEPRECATIONS.get(route or "")
    return dep.sunset if dep is not None else date.today() + timedelta(days=365)


async def report_tenant(db: AsyncSession, tenant_id: uuid.UUID, today: date | None = None) -> int:
    start, end = previous_month(today or datetime.now(UTC).date())
    begin = datetime(start.year, start.month, start.day, tzinfo=UTC)
    stop = datetime(end.year, end.month, end.day, tzinfo=UTC)
    rows = (
        await db.execute(
            select(ApiRequestLog.installation_id, ApiRequestLog.api_key_id, ApiRequestLog.route_template, func.count())
            .where(ApiRequestLog.deprecated.is_(True), ApiRequestLog.at >= begin, ApiRequestLog.at < stop)
            .group_by(ApiRequestLog.installation_id, ApiRequestLog.api_key_id, ApiRequestLog.route_template)
        )
    ).all()
    created = 0
    for inst, key, route, count in rows:
        if inst is None and key is None:
            continue
        sunset = _sunset(route)
        stmt = (
            pg_insert(ApiDeprecationNotice)
            .values(id=uuid.uuid4(), tenant_id=tenant_id, installation_id=inst, api_key_id=key, month=start,
                    route_template=(route or "")[:300], call_count=int(count), sunset=sunset, notified_at=datetime.now(UTC))
            .on_conflict_do_nothing(constraint="uq_api_deprecation_notices_key")
            .returning(ApiDeprecationNotice.id)
        )  # fmt: skip
        notice_id = (await db.execute(stmt)).scalar_one_or_none()
        if notice_id is None:
            continue
        created += 1
        emit(db, PublicApiDeprecatedUsage(tenant_id=tenant_id, notice_id=notice_id, installation_id=inst, api_key_id=key,
                                          route_template=route or "", call_count=int(count), sunset=sunset.isoformat()))  # fmt: skip
    return created


__all__ = ["previous_month", "report_tenant"]
