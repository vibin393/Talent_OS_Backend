# ruff: file-ignore[line-too-long]
"""HRMS sync records, settings and actions (Epic 71, spec A4/A11): the sync queue, the Placement panel, retry / skip /
include, "Pull status now" and the tenant settings."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.integrations import find_manifest
from app.core.pagination import Page
from app.models.candidates import Candidate
from app.models.hrms_sync import HrmsSyncRecord
from app.models.placements import Placement
from app.schemas.hrms_sync import (
    HrmsRecordOut,
    HrmsSettingsIn,
    HrmsSettingsOut,
    JobQueuedOut,
    PlacementPanelOut,
)
from app.services import integrations_service
from app.services.hrms_sync.common import category_installation, get_settings_row, require_installation
from app.services.hrms_sync.eligibility import eligible
from app.services.hrms_sync.push import ensure_record, find_record

__all__ = ["get_settings", "include", "list_records", "placement_panel", "pull_now", "put_settings", "retry", "skip"]


async def get_settings(ctx: RequestContext, db: AsyncSession) -> HrmsSettingsOut:  # ruff: ignore[unused-function-argument]
    return HrmsSettingsOut.model_validate(await get_settings_row(db))


async def put_settings(ctx: RequestContext, db: AsyncSession, data: HrmsSettingsIn) -> HrmsSettingsOut:  # ruff: ignore[unused-function-argument]
    row = await get_settings_row(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, sorted(set(value)) if key == "push_worker_types" else value)
    await db.flush()
    return HrmsSettingsOut.model_validate(row)


async def _outs(db: AsyncSession, rows: list[HrmsSyncRecord]) -> list[HrmsRecordOut]:
    cids = {r.candidate_id for r in rows}
    pids = {r.placement_id for r in rows}
    names = dict((await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cids)))).all()) if cids else {}
    hids = dict((await db.execute(select(Placement.id, Placement.human_id).where(Placement.id.in_(pids)))).all()) if pids else {}
    return [
        HrmsRecordOut.model_validate(r).model_copy(update={"worker_name": names.get(r.candidate_id), "placement_human_id": hids.get(r.placement_id)})
        for r in rows
    ]


async def list_records(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    status: str | None = None,
    legal_entity_id: uuid.UUID | None = None,
    mismatch: bool | None = None,
    page: int = 1,
    page_size: int = 25,
) -> Page[HrmsRecordOut]:
    stmt = select(HrmsSyncRecord)
    if status:
        stmt = stmt.where(HrmsSyncRecord.status == status)
    if legal_entity_id:
        stmt = stmt.where(HrmsSyncRecord.legal_entity_id == legal_entity_id)
    if mismatch is True:
        stmt = stmt.where(HrmsSyncRecord.mismatch != "none")
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list((await db.execute(stmt.order_by(HrmsSyncRecord.updated_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars())
    return Page[HrmsRecordOut](items=await _outs(db, rows), total=total, page=page, page_size=page_size)


async def _record(db: AsyncSession, record_id: uuid.UUID) -> HrmsSyncRecord:
    rec = await db.get(HrmsSyncRecord, record_id)
    if rec is None:
        raise NotFound("Sync record not found")
    return rec


async def placement_panel(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> PlacementPanelOut:  # ruff: ignore[unused-function-argument]
    p = await db.get(Placement, placement_id)
    if p is None:
        raise NotFound("Placement not found")
    ei = await category_installation(db, "hrms", p.legal_entity_id)
    rec = await find_record(db, ei.installation_id, p.id) if ei is not None else None
    settings = await get_settings_row(db)
    ok, reason = eligible(p, settings, rec)
    if ei is None:
        ok, reason = False, "no_hrms"
    return PlacementPanelOut(
        placement_id=p.id, eligible=ok, reason=reason, installation_id=ei.installation_id if ei else None,
        connector_key=ei.connector_key if ei else None, record=(await _outs(db, [rec]))[0] if rec else None, placement_status=p.status,
    )  # fmt: skip


async def enqueue_push(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, config_id: uuid.UUID, placement_id: uuid.UUID, kind: str) -> JobQueuedOut:
    from app.models.oauth import AppInstallation
    from app.services.integrations.runner import enqueue_sync

    inst = await db.get(AppInstallation, installation_id)
    m = find_manifest(inst.connector_key) if inst is not None else None
    spec = m.sync_job(f"push_{kind}") if m is not None else None
    if inst is None or m is None or spec is None:
        raise ConflictError("The HR connector cannot push", code="installation_inactive")
    job = await enqueue_sync(ctx, db, inst, m, spec, config_id=config_id, trigger="manual", subject=("placement", placement_id))
    return JobQueuedOut(job_id=job.id, status=job.status)


async def retry(ctx: RequestContext, db: AsyncSession, record_id: uuid.UUID) -> JobQueuedOut:
    rec = await _record(db, record_id)
    p = await db.get(Placement, rec.placement_id)
    if p is None:
        raise NotFound("Placement not found")
    from app.services.hrms_sync.common import entity_install

    ei = await entity_install(db, rec.installation_id, p.legal_entity_id)
    if ei is None:
        raise ConflictError("The HR connector no longer serves this legal entity", code="installation_inactive")
    kind = "exit" if rec.last_push_kind == "exit" else ("hire" if rec.employee_ref is None else "change")
    out = await enqueue_push(ctx, db, rec.installation_id, ei.config_id, p.id, kind)
    audit.record("hrms.record_retried", "hrms_sync_record", rec.id, session=db, kind=kind)
    return out


async def skip(ctx: RequestContext, db: AsyncSession, record_id: uuid.UUID, reason: str) -> HrmsRecordOut:  # ruff: ignore[unused-function-argument]
    rec = await _record(db, record_id)
    rec.status, rec.skip_reason = "skipped", reason[:40]
    await db.flush()
    audit.record("hrms.record_skipped", "hrms_sync_record", rec.id, session=db, reason=rec.skip_reason)
    return (await _outs(db, [rec]))[0]


async def include(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, value: bool) -> PlacementPanelOut:
    p = await db.get(Placement, placement_id)
    if p is None:
        raise NotFound("Placement not found")
    ei = await category_installation(db, "hrms", p.legal_entity_id)
    if ei is None:
        raise ConflictError("Install an HR system connector for this legal entity first", code="no_hrms")
    if p.payee_kind != "worker":
        raise ConflictError("Vendor-supplied workers are never pushed", code="vendor_supplied")
    rec = await ensure_record(db, ei.installation_id, p)
    rec.include_override = value
    if not value and rec.status != "synced":
        rec.status, rec.skip_reason = "skipped", "not_on_payroll"
    elif value and rec.status == "skipped":
        rec.status, rec.skip_reason = "pending", None
    await db.flush()
    audit.record("hrms.include_changed", "placement", p.id, session=db, include=value)
    if value and rec.employee_ref is None and p.status in ("active", "joining_confirmed", "offboarding"):
        await enqueue_push(ctx, db, ei.installation_id, ei.config_id, p.id, "hire")
    return await placement_panel(ctx, db, placement_id)


async def pull_now(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID) -> JobQueuedOut:
    await require_installation(db, installation_id, "hrms")
    job = await integrations_service.run_sync(ctx, db, installation_id, "pull_status")
    return JobQueuedOut(job_id=job.id, status=job.status)
