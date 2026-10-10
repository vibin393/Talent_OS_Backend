# ruff: noqa: E501
"""Partner / Tenant-developer App usage (Epic 74, story 74.5, FR-385): installs, active Installations, requests,
the error rate, webhook attempts and success, deprecated-endpoint calls by day -- aggregated across Tenants without
Tenant identities. Per-tenant rows are shown only for the owner's own sandboxes (Known gap 4: no Installation field
records a Tenant's consent to share usage yet). Same per-tenant read pattern as ``metering_service.platform_usage``
(``usage_counters`` is RLS-protected, so each Tenant is read in its own session and summed here)."""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, cast, func, select

from app.core.database import platform_session, tenant_session
from app.core.errors import ValidationFailed
from app.models.developer import DeveloperSandbox
from app.models.identity import Tenant
from app.models.metering import ApiRequestLog, UsageCounter
from app.models.oauth import AppInstallation
from app.schemas.developer import AppUsageOut, UsageDay, UsageTenantRow
from app.services.developer_portal.owner import Owner
from app.services.developer_portal.sandboxes import owner_app

ACTIVE = ("active", "degraded", "reconsent_required")


async def app_usage(owner: Owner, app_id: uuid.UUID, date_from: date, date_to: date) -> AppUsageOut:
    owner.require("viewer")
    await owner_app(owner, app_id)
    if date_to < date_from or (date_to - date_from).days > 92:
        raise ValidationFailed("Choose a range of at most 90 days", details={"fields": {"from": "Invalid range"}})
    async with platform_session() as db:
        own_sandboxes: dict[uuid.UUID, str] = dict(
            (
                await db.execute(
                    select(DeveloperSandbox.tenant_id, DeveloperSandbox.name).where(
                        DeveloperSandbox.tenant_id.is_not(None), DeveloperSandbox.status != "deleted",
                        (DeveloperSandbox.partner_org_id == owner.id) if owner.kind == "partner_org" else (DeveloperSandbox.owner_tenant_id == owner.id),
                    )
                )
            ).all()  # type: ignore[arg-type]
        )  # fmt: skip
        production = list((await db.execute(select(Tenant.id).where(Tenant.mode != "sandbox", Tenant.status == "active"))).scalars())
    if owner.kind == "tenant":
        production = [t for t in production if t == owner.id]
    days: dict[date, UsageDay] = {date_from + timedelta(days=i): UsageDay(day=date_from + timedelta(days=i)) for i in range((date_to - date_from).days + 1)}
    installs_total = installs_active = 0
    per_tenant: dict[str, int] = defaultdict(int)
    start = datetime.combine(date_from, time.min, tzinfo=UTC)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
    for tenant_id in [*production, *own_sandboxes]:
        async with tenant_session(tenant_id) as tdb:
            insts = list((await tdb.execute(select(AppInstallation.id, AppInstallation.status).where(AppInstallation.app_id == app_id))).all())
            if not insts:
                continue
            installs_total += sum(1 for _, s in insts if s != "uninstalled")
            installs_active += sum(1 for _, s in insts if s in ACTIVE)
            rows = await tdb.execute(
                select(UsageCounter.day, UsageCounter.unit, func.sum(UsageCounter.count), func.sum(UsageCounter.error_count))
                .where(UsageCounter.app_id == app_id, UsageCounter.day >= date_from, UsageCounter.day <= date_to)
                .group_by(UsageCounter.day, UsageCounter.unit)
            )
            for day, unit, count, errors in rows.all():
                d = days.get(day)
                if d is None:
                    continue
                if unit == "api_request":
                    d.requests += int(count or 0)
                    d.errors += int(errors or 0)
                    if tenant_id in own_sandboxes:
                        per_tenant[f"Sandbox: {own_sandboxes[tenant_id]}"] += int(count or 0)
                elif unit == "webhook_attempt":
                    d.webhook_attempts += int(count or 0)
                    d.webhook_failures += int(errors or 0)
            inst_ids = [i for i, _ in insts]
            dep = await tdb.execute(
                select(cast(ApiRequestLog.at, Date), func.count())
                .where(ApiRequestLog.installation_id.in_(inst_ids), ApiRequestLog.deprecated.is_(True), ApiRequestLog.at >= start, ApiRequestLog.at < end)
                .group_by(cast(ApiRequestLog.at, Date))
            )
            for day, count in dep.all():
                if day in days:
                    days[day].deprecated_calls += int(count or 0)
    series = list(days.values())
    requests = sum(d.requests for d in series)
    errors = sum(d.errors for d in series)
    attempts = sum(d.webhook_attempts for d in series)
    failures = sum(d.webhook_failures for d in series)
    return AppUsageOut(
        app_id=app_id, date_from=date_from, date_to=date_to, installs_total=installs_total, installs_active=installs_active, requests=requests,
        error_rate=round(errors / requests, 4) if requests else 0.0, webhook_attempts=attempts,
        webhook_success_rate=round((attempts - failures) / attempts, 4) if attempts else None,
        deprecated_calls=sum(d.deprecated_calls for d in series), days=series,
        tenants=[UsageTenantRow(label=k, requests=v) for k, v in sorted(per_tenant.items())],
    )  # fmt: skip


__all__ = ["app_usage"]
