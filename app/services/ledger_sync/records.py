# ruff: file-ignore[line-too-long]
"""Ledger sync records, runs and settings (Epic 71, spec A8/A11): the Documents tab, the Invoice / note panel,
retry / skip, runs (push / Tally file / pull payments / masters) and the tenant settings.

Runs execute inside the request (bounded batches, at most 200 documents per push run); the connector calls go through
the Egress Guard with their own timeouts. Scheduled pulls and event pushes use the connector job runner."""

import uuid
from datetime import date
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.pagination import Page
from app.core.permissions import Principal
from app.models.billing import CreditNote, DebitNote, Invoice
from app.models.clients import Client
from app.models.identity import User
from app.models.ledger_sync import LedgerRun, LedgerSyncRecord
from app.models.receivables import Payment
from app.schemas.ledger_sync import (
    DocumentPanelOut,
    LedgerRecordOut,
    LedgerRunOut,
    LedgerSettingsIn,
    LedgerSettingsOut,
    RunIn,
)
from app.services import files_service, integrations_service
from app.services.ledger_sync import documents, tally
from app.services.ledger_sync.common import (
    EntityInstall,
    category_installation,
    entity_install,
    get_settings_row,
    now,
)
from app.services.ledger_sync.mappings import mapping_set
from app.services.ledger_sync.push import push_one, push_records

__all__ = ["create_run", "document_panel", "get_settings", "list_records", "list_runs", "put_settings", "retry", "run_file", "skip"]

_DOCS = {"invoice": Invoice, "credit_note": CreditNote, "debit_note": DebitNote}


async def get_settings(ctx: RequestContext, db: AsyncSession) -> LedgerSettingsOut:  # ruff: ignore[unused-function-argument]
    return LedgerSettingsOut.model_validate(await get_settings_row(db))


async def put_settings(ctx: RequestContext, db: AsyncSession, data: LedgerSettingsIn) -> LedgerSettingsOut:  # ruff: ignore[unused-function-argument]
    row = await get_settings_row(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    return LedgerSettingsOut.model_validate(row)


async def _outs(db: AsyncSession, rows: list[LedgerSyncRecord]) -> list[LedgerRecordOut]:
    out: list[LedgerRecordOut] = []
    client_ids: set[uuid.UUID] = set()
    docs: dict[uuid.UUID, object] = {}
    for kind, model in _DOCS.items():
        model = cast("type[Invoice]", model)
        ids = [r.document_id for r in rows if r.document_kind == kind]
        if ids:
            for d in (await db.execute(select(model).where(model.id.in_(ids)))).scalars():
                docs[d.id] = d
                client_ids.add(d.client_id)
    pids = [r.document_id for r in rows if r.document_kind == "payment"]
    if pids:
        for p in (await db.execute(select(Payment).where(Payment.id.in_(pids)))).scalars():
            docs[p.id] = p
            client_ids.add(p.client_id)
    names = dict((await db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids)))).tuples().all()) if client_ids else {}
    for r in rows:
        d = docs.get(r.document_id)  # type: ignore[assignment]
        number = getattr(d, "number", None) or getattr(d, "human_id", None)
        amount = getattr(d, "document_total", None) if r.document_kind != "payment" else getattr(d, "amount", None)
        out.append(
            LedgerRecordOut.model_validate(r).model_copy(
                update={"document_number": number, "client_name": names.get(getattr(d, "client_id", None)), "amount": amount, "currency": getattr(d, "currency", None)}  # type: ignore[arg-type]
            )
        )
    return out


async def list_records(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    status: str | None = None,
    kind: str | None = None,
    legal_entity_id: uuid.UUID | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    page: int = 1,
    page_size: int = 25,
) -> Page[LedgerRecordOut]:
    stmt = select(LedgerSyncRecord)
    if status:
        stmt = stmt.where(LedgerSyncRecord.status == status)
    if kind:
        stmt = stmt.where(LedgerSyncRecord.document_kind == kind)
    if legal_entity_id:
        stmt = stmt.where(LedgerSyncRecord.legal_entity_id == legal_entity_id)
    if from_date:
        stmt = stmt.where(LedgerSyncRecord.doc_date >= from_date)
    if to_date:
        stmt = stmt.where(LedgerSyncRecord.doc_date <= to_date)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list((await db.execute(stmt.order_by(LedgerSyncRecord.doc_date.desc(), LedgerSyncRecord.updated_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars())
    return Page[LedgerRecordOut](items=await _outs(db, rows), total=total, page=page, page_size=page_size)


async def _record(db: AsyncSession, record_id: uuid.UUID) -> LedgerSyncRecord:
    rec = await db.get(LedgerSyncRecord, record_id)
    if rec is None:
        raise NotFound("Sync record not found")
    return rec


async def document_panel(ctx: RequestContext, db: AsyncSession, kind: str, document_id: uuid.UUID) -> DocumentPanelOut:  # ruff: ignore[unused-function-argument]
    if kind not in ("invoice", "credit_note", "debit_note", "payment"):
        raise NotFound("Document not found")
    doc = await documents.load_document(db, kind, document_id)
    if doc is None:
        raise NotFound("Document not found")
    ei = await category_installation(db, "accounting", doc.legal_entity_id)
    rec = None
    if ei is not None:
        rec = (
            await db.execute(select(LedgerSyncRecord).where(LedgerSyncRecord.installation_id == ei.installation_id, LedgerSyncRecord.document_kind == kind, LedgerSyncRecord.document_id == document_id))
        ).scalar_one_or_none()
    return DocumentPanelOut(document_kind=kind, document_id=document_id, record=(await _outs(db, [rec]))[0] if rec else None, connector_key=ei.connector_key if ei else None)


async def retry(ctx: RequestContext, db: AsyncSession, record_id: uuid.UUID) -> LedgerRecordOut:
    rec = await _record(db, record_id)
    ei = await entity_install(db, rec.installation_id, rec.legal_entity_id)
    if ei is None:
        raise ConflictError("The accounting connector no longer serves this legal entity", code="installation_inactive")
    if ei.connector_key == "tally_xml":
        raise ConflictError("Generate a Tally file for the period instead", code="tally_file_only")
    doc = await documents.load_document(db, rec.document_kind, rec.document_id)
    if doc is None:
        raise NotFound("Document not found")
    if rec.status == "skipped":
        rec.status, rec.skip_reason, rec.skipped_by = "pending", None, None
    adapter = await integrations_service.adapter_for(ctx, db, ei.installation_id, config_id=ei.config_id)
    await push_one(ctx, db, ei, adapter, rec.document_kind, doc, await mapping_set(db, ei.installation_id, rec.legal_entity_id))
    await db.refresh(rec)
    audit.record("ledger.record_retried", "ledger_sync_record", rec.id, session=db, status=rec.status)
    return (await _outs(db, [rec]))[0]


def _user(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


async def skip(ctx: RequestContext, db: AsyncSession, record_id: uuid.UUID, reason: str) -> LedgerRecordOut:
    rec = await _record(db, record_id)
    rec.status, rec.skip_reason, rec.skipped_by = "skipped", reason.strip()[:200], _user(ctx)
    await db.flush()
    audit.record("ledger.record_skipped", "ledger_sync_record", rec.id, session=db)
    return (await _outs(db, [rec]))[0]


async def _run_out(db: AsyncSession, runs: list[LedgerRun]) -> list[LedgerRunOut]:
    uids = {r.created_by for r in runs if r.created_by}
    names = dict((await db.execute(select(User.id, User.name).where(User.id.in_(uids)))).all()) if uids else {}  # type: ignore[arg-type, var-annotated]
    return [LedgerRunOut.model_validate(r).model_copy(update={"created_by_name": names.get(r.created_by) if r.created_by else None}) for r in runs]


async def _install_for(db: AsyncSession, data: RunIn) -> EntityInstall:
    if data.installation_id is not None:
        ei = await entity_install(db, data.installation_id, data.legal_entity_id)
    else:
        ei = await category_installation(db, "accounting", data.legal_entity_id)
    if ei is None:
        raise ConflictError("Install an accounting connector for this legal entity first", code="no_accounting_connector")
    return ei


async def create_run(ctx: RequestContext, db: AsyncSession, data: RunIn) -> LedgerRunOut:
    ei = await _install_for(db, data)
    tally_only = ei.connector_key == "tally_xml"
    if (data.kind == "tally_file") != tally_only and data.kind in ("tally_file", "push", "pull_payments", "status"):
        raise ValidationFailed("This run is not available for the installed connector", code="run_kind_unsupported", details={"kind": data.kind})
    run = LedgerRun(
        id=uuid.uuid4(), tenant_id=ei.installation.tenant_id, installation_id=ei.installation_id, legal_entity_id=data.legal_entity_id, kind=data.kind,
        from_date=data.from_date, to_date=data.to_date, include_masters=data.include_masters, status="running", counts={}, created_by=_user(ctx),
    )  # fmt: skip
    db.add(run)
    await db.flush()
    try:
        if data.kind == "push":
            run.counts = await push_records(ctx, db, ei, data.legal_entity_id, data.from_date, data.to_date)
        elif data.kind == "tally_file":
            await tally.generate(ctx, db, ei, run)
        elif data.kind == "pull_payments":
            job = await integrations_service.run_sync(ctx, db, ei.installation_id, "pull_payments")
            run.counts = {"job_id": str(job.id)}
        elif data.kind == "masters":
            from app.services.ledger_sync.mappings import load_masters

            masters = await load_masters(ctx, db, ei.installation_id, data.legal_entity_id)
            run.counts = {"options": len(masters.options)}
        else:
            run.counts = {}
        run.status = "completed"
    except ConflictError:
        raise
    except Exception as exc:
        run.status = "failed"
        run.counts = {**dict(run.counts or {}), "error": type(exc).__name__}
        await db.flush()
        audit.record("ledger.run_failed", "ledger_run", run.id, session=db, kind=run.kind)
        raise
    run.finished_at = now()
    await db.flush()
    audit.record("ledger.run_completed", "ledger_run", run.id, session=db, kind=run.kind, counts={k: v for k, v in run.counts.items() if isinstance(v, int)})
    return (await _run_out(db, [run]))[0]


async def list_runs(ctx: RequestContext, db: AsyncSession, *, legal_entity_id: uuid.UUID | None = None, kind: str | None = None, page: int = 1, page_size: int = 25) -> Page[LedgerRunOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(LedgerRun)
    if legal_entity_id:
        stmt = stmt.where(LedgerRun.legal_entity_id == legal_entity_id)
    if kind:
        stmt = stmt.where(LedgerRun.kind == kind)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list((await db.execute(stmt.order_by(LedgerRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars())
    return Page[LedgerRunOut](items=await _run_out(db, rows), total=total, page=page, page_size=page_size)


async def run_file(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> files_service.Download:  # ruff: ignore[unused-function-argument]
    run = await db.get(LedgerRun, run_id)
    if run is None or run.file_id is None:
        raise NotFound("File not found")
    stored = await files_service.get_file(db, run.file_id)
    if stored is None:
        raise NotFound("File not found")
    audit.record("ledger.tally_file_downloaded", "ledger_run", run.id, session=db)
    return files_service.Download(data=await files_service.read_file(stored), filename=str(stored.original_name), content_type="application/xml")
