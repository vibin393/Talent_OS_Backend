# ruff: file-ignore[line-too-long]
"""Payroll push (Epic 71, story 71.1, spec A6; P2 FR-142): the ``payroll_push`` sync job of ``darwinbox_payroll`` /
``keka_payroll`` uploads a finalised Payroll Export through ``PayrollPushPort.push_export`` and records the provider
status through the P2 facade (``payroll_service.set_provider_status``). ``ManualActionRequired`` -> the version stays
``not_sent`` and a Task asks a person to upload the file. P2's legacy ``_queue_push`` skips Installation configs
(D-18), so a version is pushed once."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.integrations import ConnectorPermanentError, ManualActionRequired
from app.models.payroll import PayPeriod, PayrollExportVersion
from app.services import integrations_service, payroll_service
from app.services.hrms_sync.common import entity_install, fix_it_task, sanitise

__all__ = ["payroll_push"]


async def payroll_push(ctx: RequestContext, db: AsyncSession, run: Any) -> integrations_service.SyncResult:
    if run.subject is None or run.subject[0] != "payroll_export":
        return integrations_service.SyncResult()
    version_id = run.subject[1]
    v = await db.get(PayrollExportVersion, version_id)
    if v is None or v.kind == "preview":
        return integrations_service.SyncResult(skipped=1)
    if v.provider_status in ("sent", "processed"):
        return integrations_service.SyncResult(skipped=1)  # replay
    ei = await entity_install(db, run.installation_id, v.legal_entity_id)
    if ei is None:
        return integrations_service.SyncResult(skipped=1)
    adapter = run.adapter if run.config_id == ei.config_id else await integrations_service.adapter_for(ctx, db, run.installation_id, config_id=ei.config_id)
    data, media_type = await payroll_service.export_file_bytes(ctx, db, version_id)
    try:
        result = await adapter.push_export(version_id, data, media_type)
    except ManualActionRequired:
        period = await db.get(PayPeriod, v.pay_period_id)
        label = f"{period.period_start:%b %Y}" if period is not None else v.human_id
        await fix_it_task(
            db, owner_id=ei.installation.accountable_user_id, entity_type="installation", entity_id=ei.installation_id,
            title=f"Upload payroll file for {label} to {ei.installation.display_name}",
            notes=f"Download {v.human_id} from Payroll exports and upload it in the payroll system.", fallback_permission="payroll:read",
        )  # fmt: skip
        return integrations_service.SyncResult(processed=1, skipped=1)
    except ConnectorPermanentError as exc:
        await payroll_service.set_provider_status(ctx, db, version_id, "failed", None)
        return integrations_service.SyncResult(
            processed=1, failed=1, errors=(integrations_service.SyncError("payroll_export", version_id, None, exc.code, sanitise(str(exc)) or exc.code),)
        )
    ref = (str(result.get("external_ref") or "") or None) if isinstance(result, Mapping) else None
    await payroll_service.set_provider_status(ctx, db, version_id, "sent", ref)
    return integrations_service.SyncResult(processed=1, updated=1)
