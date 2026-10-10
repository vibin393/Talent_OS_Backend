# ruff: noqa: ARG001, E501
"""Daily expiry recompute and alerts (Epic 21 A6, P2-8.3, FR-115).

``run(ctx, db, at)`` runs once per tenant-local date (``compliance_recompute_runs`` UNIQUE ``run_date``) when the
local hour equals ``recompute_hour_local`` (or ``force``):

1. recompute ``expiry_status`` of valid, current credentials (``expiry.status_for``);
2. refresh every open placement's checklist and flags (events ``CredentialStatusChanged`` /
   ``PlacementComplianceChanged``; placements' handler sets ``compliance_blocked``);
3. alerts, deduped by ``credential_alerts(credential_id, offset_days)``: ``CredentialExpiring`` at each offset and
   ``CredentialExpired`` once (notification types ``compliance.credential_expiring|expired``).

Timesheets are never blocked: nothing here (or anywhere in ``compliance``) touches ``time``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.fiscal import local_date
from app.models.compliance import ComplianceRecomputeRun, Credential, CredentialAlert, CredentialType
from app.services.compliance import checklist, expiry
from app.services.compliance.common import (
    CredentialExpired,
    CredentialExpiring,
    get_settings_row,
    now,
    tenant_of,
)


async def run(
    ctx: RequestContext, db: AsyncSession, at: datetime | None = None, *, force: bool = False
) -> dict[str, Any]:
    at = at or datetime.now(UTC)
    settings = await get_settings_row(db)
    tz = settings.timezone or "Asia/Kolkata"
    run_date = local_date(at, tz)
    if not force and at.astimezone(ZoneInfo(tz)).hour != settings.recompute_hour_local:
        return {"skipped": "not_due"}
    inserted = (
        await db.execute(
            pg_insert(ComplianceRecomputeRun)
            .values(id=uuid.uuid4(), tenant_id=tenant_of(db), run_date=run_date)
            .on_conflict_do_nothing(constraint="uq_compliance_recompute_runs_run_date")
            .returning(ComplianceRecomputeRun.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return {"skipped": "already_ran", "run_date": run_date.isoformat()}

    types = {t.id: t for t in (await db.execute(select(CredentialType))).scalars()}
    default = list(settings.default_alert_offsets)
    creds = list(
        (
            await db.execute(
                select(Credential).where(
                    Credential.verification_status == "valid",
                    Credential.superseded_at.is_(None),
                    Credential.erased_at.is_(None),
                )
            )
        ).scalars()
    )
    changed = 0
    alerts = 0
    for c in creds:
        t = types.get(c.credential_type_id)
        offsets = list(t.alert_offsets) if t is not None and t.alert_offsets else default
        new = expiry.status_for(c.expires_on, run_date, offsets)
        if new != c.expiry_status:
            c.expiry_status = new
            changed += 1
        sent = set(
            (
                await db.execute(
                    select(CredentialAlert.offset_days).where(CredentialAlert.credential_id == c.id)
                )
            ).scalars()
        )
        target, mark = expiry.alert_plan(c.expires_on, run_date, offsets, sent)
        for o in mark:
            await db.execute(
                pg_insert(CredentialAlert)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=c.tenant_id,
                    credential_id=c.id,
                    candidate_id=c.candidate_id,
                    offset_days=o,
                )
                .on_conflict_do_nothing(constraint="uq_credential_alerts_key")
            )
        if target is None:
            continue
        alerts += 1
        if target == expiry.EXPIRED_OFFSET:
            emit(
                db,
                CredentialExpired(
                    tenant_id=c.tenant_id,
                    credential_id=c.id,
                    candidate_id=c.candidate_id,
                    type_key=c.type_key,
                    expires_on=c.expires_on,
                ),
            )
        else:
            emit(
                db,
                CredentialExpiring(
                    tenant_id=c.tenant_id,
                    credential_id=c.id,
                    candidate_id=c.candidate_id,
                    type_key=c.type_key,
                    offset_days=target,
                    expires_on=c.expires_on,
                ),
            )
    await db.flush()
    placements = await checklist.refresh_all(db, on_date=run_date)
    stats = {"credentials_changed": changed, "alerts": alerts, "placements": placements}
    row = await db.get(ComplianceRecomputeRun, inserted)
    if row is not None:
        row.finished_at = now()
        row.stats = stats
    await db.flush()
    return {**stats, "run_date": run_date.isoformat()}
