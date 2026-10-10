# ruff: noqa: E501, ARG001
"""New-hire, change and exit push (Epic 71, story 71.2, spec A4).

Sync handlers (``register_sync_handler("hrms", "push_hire" | "push_change" | "push_exit" | "manual_push", fn)``) run as
``connector_jobs`` on queue ``ecosystem``; the internal outbox events ``placement.joined`` / ``placement.changed`` and
the public ``placement.ended`` trigger them (``connector_triggers`` sink). Every push is idempotent per Placement and
``lock_version`` (``pushed_version``) and never creates a second employee (``employee_ref`` + ``connector_links``).

Nothing in Talent OS changes from provider data: the record (ids, statuses, sanitised message) is the only write.
"""

import hashlib
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.core.integrations import ConnectorPermanentError, ManualActionRequired
from app.events.hrms_sync import HrmsSyncStatusChanged
from app.models.candidates import Candidate
from app.models.hrms_sync import HrmsSyncRecord
from app.models.placements import Placement, RateSegment
from app.services import exposure_service, integrations_service
from app.services.connectors.hrms.port import ChangeDTO, HireDTO, HrmsResultDTO
from app.services.hrms_sync import mappings
from app.services.hrms_sync.catalogue import placement_values
from app.services.hrms_sync.common import (
    EntityInstall,
    entity_install,
    fix_it_task,
    get_settings_row,
    now,
    sanitise,
)
from app.services.hrms_sync.eligibility import eligible

__all__ = ["KINDS", "ensure_record", "push_placement", "sync_handler"]

KINDS = ("hire", "change", "exit")
SyncResult = integrations_service.SyncResult
SyncError = integrations_service.SyncError


async def ensure_record(db: AsyncSession, installation_id: uuid.UUID, p: Placement) -> HrmsSyncRecord:
    await db.execute(
        pg_insert(HrmsSyncRecord)
        .values(
            id=uuid.uuid4(), tenant_id=p.tenant_id, installation_id=installation_id, legal_entity_id=p.legal_entity_id,
            placement_id=p.id, candidate_id=p.candidate_id,
        )
        .on_conflict_do_nothing(constraint="uq_hrms_sync_records_placement")
    )  # fmt: skip
    return (
        await db.execute(
            select(HrmsSyncRecord).where(HrmsSyncRecord.installation_id == installation_id, HrmsSyncRecord.placement_id == p.id).with_for_update()
        )
    ).scalar_one()


async def find_record(db: AsyncSession, installation_id: uuid.UUID, placement_id: uuid.UUID) -> HrmsSyncRecord | None:
    return (
        await db.execute(select(HrmsSyncRecord).where(HrmsSyncRecord.installation_id == installation_id, HrmsSyncRecord.placement_id == placement_id))
    ).scalar_one_or_none()


def _status(db: AsyncSession, rec: HrmsSyncRecord, status: str, *, message: str | None = None) -> None:
    changed = rec.status != status
    rec.status = status
    rec.last_message = sanitise(message)
    if changed:
        emit(db, HrmsSyncStatusChanged(tenant_id=rec.tenant_id, record_id=rec.id, placement_id=rec.placement_id, status=status))


async def _worker_name(db: AsyncSession, candidate_id: uuid.UUID) -> str:
    cand = await db.get(Candidate, candidate_id)
    return str(getattr(cand, "full_name", None) or "the worker")


async def _effective_date(db: AsyncSession, p: Placement) -> date:
    seg = (
        await db.execute(
            select(RateSegment.effective_from)
            .where(RateSegment.placement_id == p.id, RateSegment.status == "active")
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return seg or p.actual_start_date or p.planned_start_date


def _hashes(fields: dict[str, Any]) -> dict[str, str]:
    return {k: hashlib.sha256(str(v).encode()).hexdigest()[:16] for k, v in fields.items()}


async def _notice(ctx: RequestContext, db: AsyncSession, rec: HrmsSyncRecord, ei: EntityInstall, fields: dict[str, Any]) -> None:
    """``hrms_transfer`` notice: recorded once per Worker (consent evidence, source ``placement``)."""
    from app.services.privacy import consent

    try:
        async with db.begin_nested():
            await consent.record(
                db, rec.candidate_id, "hrms_transfer", "placement", None, None,
                evidence={"installation_id": str(ei.installation_id), "connector": ei.connector_key, "fields": sorted(fields)},
            )  # fmt: skip
    except Exception:  # pragma: no cover - the push itself succeeded; the notice is retried on the next push
        from loguru import logger

        logger.exception("hrms_transfer notice for record {} not recorded", rec.id)


async def _blocked(db: AsyncSession, rec: HrmsSyncRecord, ei: EntityInstall, missing: list[str], p: Placement) -> SyncResult:
    rec.blocked_fields = list(missing)
    _status(db, rec, "blocked", message=f"Map {', '.join(missing)} before pushing")
    name = await _worker_name(db, p.candidate_id)
    await fix_it_task(
        db, owner_id=ei.installation.accountable_user_id, entity_type="hrms_sync", entity_id=rec.id,
        title=f"Map {', '.join(missing)} before pushing {name}",
        notes=f"Fix mapping: /settings/hrms?installation_id={ei.installation_id}&legal_entity_id={p.legal_entity_id}",
    )  # fmt: skip
    await db.flush()
    return SyncResult(processed=1, skipped=1, errors=(SyncError("worker", p.id, None, "mapping_missing", "Required HRMS fields are not mapped"),))


async def _apply_result(
    ctx: RequestContext, db: AsyncSession, rec: HrmsSyncRecord, ei: EntityInstall, p: Placement, kind: str, result: HrmsResultDTO, fields: dict[str, Any]
) -> SyncResult:
    rec.last_push_kind = kind
    rec.last_pushed_at = now()
    rec.blocked_fields = []
    if result.status == "failed":
        _status(db, rec, "failed", message=result.message or "The HR system refused the record")
        return SyncResult(processed=1, failed=1)
    if result.employee_ref and rec.employee_ref is None:
        rec.employee_ref = result.employee_ref[:200]
        await integrations_service.link(db, ei.installation_id, "worker", p.id, rec.employee_ref, candidate_id=p.candidate_id, local_version=str(p.lock_version))
        await exposure_service.record(ctx, subject_type="candidate", subject_id=p.candidate_id, channel="connector", installation_id=ei.installation_id)
        await _notice(ctx, db, rec, ei, fields)
        audit.record("hrms.worker_pushed", "placement", p.id, session=db, installation_id=str(ei.installation_id), fields=sorted(fields))
    rec.pushed_version = max(rec.pushed_version, p.lock_version)
    rec.field_hashes = {**dict(rec.field_hashes or {}), **_hashes(fields)}
    if result.status == "pending":
        _status(db, rec, "pending", message=result.message)
        return SyncResult(processed=1, skipped=1)
    _status(db, rec, "synced", message=None)
    await db.flush()
    return SyncResult(processed=1, created=1 if kind == "hire" else 0, updated=0 if kind == "hire" else 1)


async def push_placement(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, placement_id: uuid.UUID, kind: str, *, adapter: Any = None, config_id: uuid.UUID | None = None, force: bool = False) -> SyncResult:
    """Push one Placement (``kind`` hire | change | exit). ``force`` (retry / manual) ignores the push_* settings."""
    p = await db.get(Placement, placement_id)
    if p is None:
        return SyncResult(skipped=1)
    ei = await entity_install(db, installation_id, p.legal_entity_id)
    if ei is None:
        return SyncResult(skipped=1)  # this Installation does not serve the Placement's Legal Entity
    settings = await get_settings_row(db)
    if not force and ((kind == "hire" and not settings.push_on_join) or (kind == "change" and not settings.push_changes) or (kind == "exit" and not settings.push_exits)):
        return SyncResult(skipped=1)
    rec = await find_record(db, installation_id, p.id)
    ok, reason = eligible(p, settings, rec)
    if not ok:
        if rec is not None and rec.status != "synced":
            rec.skip_reason = reason
            _status(db, rec, "skipped", message=None)
            await db.flush()
        return SyncResult(skipped=1)
    rec = await ensure_record(db, installation_id, p)
    if adapter is None or config_id != ei.config_id:
        adapter = await integrations_service.adapter_for(ctx, db, installation_id, config_id=ei.config_id)
    if kind == "change" and rec.employee_ref is None:
        kind = "hire"
    try:
        if kind == "exit":
            if rec.employee_ref is None:
                rec.blocked_fields = []
                _status(db, rec, "blocked", message="Hire was never pushed")
                await db.flush()
                return SyncResult(processed=1, skipped=1)
            last_day = p.end_date or date.today()
            result = await adapter.push_exit(rec.employee_ref, last_day)
            return await _apply_result(ctx, db, rec, ei, p, "exit", result, {})
        rows = await mappings.load_rows(db, installation_id, p.legal_entity_id)
        schema = await adapter.field_schema()
        required = [f.key for f in schema.fields if f.required]
        values = await placement_values(db, p, mappings.wanted_keys(rows))
        fields, missing = mappings.build_fields(rows, values, required)
        if missing:
            return await _blocked(db, rec, ei, missing, p)
        if kind == "hire":
            if rec.employee_ref is not None and rec.pushed_version >= p.lock_version:
                return SyncResult(skipped=1)  # replay: no second employee
            if rec.employee_ref is not None:
                kind = "change"
            else:
                hire = HireDTO(worker_id=p.candidate_id, placement_id=p.id, placement_version=p.lock_version, legal_entity_id=p.legal_entity_id, fields=fields)
                result = await adapter.push_hire(hire)
                return await _apply_result(ctx, db, rec, ei, p, "hire", result, fields)
        old = dict(rec.field_hashes or {})
        changed = {k: v for k, v in fields.items() if old.get(k) != _hashes({k: v})[k]}
        if not changed:
            rec.pushed_version = max(rec.pushed_version, p.lock_version)
            await db.flush()
            return SyncResult(skipped=1)
        assert rec.employee_ref is not None
        change = ChangeDTO(worker_id=p.candidate_id, placement_id=p.id, placement_version=p.lock_version, employee_ref=rec.employee_ref, fields=changed, effective_date=await _effective_date(db, p))
        result = await adapter.push_change(change)
        return await _apply_result(ctx, db, rec, ei, p, "change", result, changed)
    except ConnectorPermanentError as exc:
        rec.last_push_kind, rec.last_pushed_at = kind, now()
        _status(db, rec, "failed", message=str(exc))
        await db.flush()
        return SyncResult(processed=1, failed=1, errors=(SyncError("worker", p.id, rec.employee_ref, exc.code, sanitise(str(exc)) or exc.code),))
    except ManualActionRequired as exc:
        rec.last_push_kind = kind
        _status(db, rec, "pending", message=exc.instructions)
        await db.flush()
        return SyncResult(processed=1, skipped=1)


def sync_handler(kind: str) -> Any:
    async def handler(ctx: RequestContext, db: AsyncSession, run: Any) -> SyncResult:
        if run.subject is None or run.subject[0] != "placement":
            return SyncResult()
        return await push_placement(ctx, db, run.installation_id, run.subject[1], kind, adapter=run.adapter, config_id=run.config_id, force=run.trigger == "manual")

    handler.__qualname__ = f"hrms_push_{kind}"
    return handler


async def manual_push(ctx: RequestContext, db: AsyncSession, run: Any) -> SyncResult:
    """``manual_push``: every ``pending`` / ``failed`` record of the Installation (hire, else change), in one run."""
    recs = list(
        (
            await db.execute(
                select(HrmsSyncRecord).where(HrmsSyncRecord.installation_id == run.installation_id, HrmsSyncRecord.status.in_(("pending", "failed")))
            )
        ).scalars()
    )
    total = SyncResult()
    for rec in recs[:50]:
        r = await push_placement(ctx, db, run.installation_id, rec.placement_id, "hire" if rec.employee_ref is None else "change", force=True)
        total = SyncResult(
            processed=total.processed + r.processed, created=total.created + r.created, updated=total.updated + r.updated,
            skipped=total.skipped + r.skipped, failed=total.failed + r.failed, errors=(*total.errors, *r.errors),
        )  # fmt: skip
    return total
