# ruff: file-ignore[line-too-long, unused-function-argument]
"""Finance document push (Epic 71, story 71.5, spec A8).

* Event sync job ``push_document`` (``invoice.issued``, internal ``credit_note.issued`` / ``debit_note.issued`` /
  ``invoice.cancelled``, ``payment.recorded``): with ``auto_push_on_issue`` the document is pushed at once, otherwise
  it waits as a ``pending`` record for "Sync now".
* ``push_records``: pushes ``pending | failed | out_of_sync`` records of a Legal Entity / range in batches.
* Rules: drafts never push; a document dated in a closed Accounting Period -> ``blocked_period``; one push per
  document version (``synced_version``; a cancel bumps the version -> void); a missing mapping -> ``failed`` with
  "Missing mapping: {kind} {key}" and one fix-it Task per missing key; customers missing in Zoho Books are created
  (``masters_sync``) on first push; **echo rule**: a Payment linked to a ledger payment (confirmed suggestion) is never
  pushed back.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.core.integrations import ConnectorPermanentError, ManualActionRequired
from app.events.ledger_sync import LedgerSyncStatusChanged
from app.models.billing import CreditNote, DebitNote, Invoice
from app.models.clients import Client
from app.models.ledger_sync import LedgerSyncRecord
from app.models.receivables import Payment
from app.services import integrations_service, legal_entities_service
from app.services.ledger_sync import documents
from app.services.ledger_sync.common import (
    EntityInstall,
    entity_install,
    fix_it_task,
    get_settings_row,
    now,
    sanitise,
)
from app.services.ledger_sync.mappings import MappingSet, mapping_set, save_mapping

__all__ = ["discover", "ensure_record", "push_document_handler", "push_records", "set_status"]

SyncResult = integrations_service.SyncResult
SyncError = integrations_service.SyncError
PUSHABLE = ("pending", "failed", "out_of_sync")
BATCH = 25
_MODELS = {"invoice": Invoice, "credit_note": CreditNote, "debit_note": DebitNote}


def set_status(db: AsyncSession, rec: LedgerSyncRecord, status: str, *, message: str | None = None) -> None:
    changed = rec.status != status
    rec.status = status
    rec.message = sanitise(message)
    if changed:
        emit(db, LedgerSyncStatusChanged(tenant_id=rec.tenant_id, record_id=rec.id, document_kind=rec.document_kind, document_id=rec.document_id, status=status))


async def ensure_record(db: AsyncSession, installation_id: uuid.UUID, kind: str, doc: Any) -> LedgerSyncRecord:
    await db.execute(
        pg_insert(LedgerSyncRecord)
        .values(
            id=uuid.uuid4(), tenant_id=doc.tenant_id, installation_id=installation_id, legal_entity_id=doc.legal_entity_id,
            document_kind=kind, document_id=doc.id, document_version=documents.doc_version(kind, doc), doc_date=documents.doc_date(kind, doc),
        )
        .on_conflict_do_nothing(constraint="uq_ledger_sync_records_doc")
    )  # fmt: skip
    rec = (
        await db.execute(
            select(LedgerSyncRecord).where(LedgerSyncRecord.installation_id == installation_id, LedgerSyncRecord.document_kind == kind, LedgerSyncRecord.document_id == doc.id).with_for_update()
        )
    ).scalar_one()
    version = documents.doc_version(kind, doc)
    if version > rec.document_version:
        rec.document_version = version
        if rec.status == "synced":
            set_status(db, rec, "out_of_sync", message="Cancelled in Talent OS; the next sync updates the ledger")
    return rec


def _pushable(kind: str, doc: Any) -> bool:
    if kind == "payment":
        return True
    return doc.status in ("issued", "cancelled") and bool(doc.number) and doc.doc_date is not None


async def _period_open(db: AsyncSession, legal_entity_id: uuid.UUID, on: date) -> bool:
    try:
        async with db.begin_nested():
            await legal_entities_service.assert_open(db, legal_entity_id, on)
    except ConflictError:
        return False
    return True


async def _missing(db: AsyncSession, rec: LedgerSyncRecord, ei: EntityInstall, missing: Sequence[tuple[str, str]]) -> None:
    set_status(db, rec, "failed", message="; ".join(f"Missing mapping: {k} {key}" for k, key in missing)[:500])
    from app.models.legal_entities import LegalEntity

    le = await db.get(LegalEntity, rec.legal_entity_id)
    entity = getattr(le, "legal_name", None) or getattr(le, "name", None) or "the legal entity"
    for kind, key in missing:
        await fix_it_task(
            db, owner_id=ei.installation.accountable_user_id, entity_type="ledger_sync", entity_id=ei.installation_id,
            title=f"Map {kind} {key} for {entity}"[:200],
            notes=f"Ledger mapping: /settings/ledger?installation_id={ei.installation_id}&legal_entity_id={rec.legal_entity_id}",
        )  # fmt: skip


async def _ensure_customer(ctx: RequestContext, db: AsyncSession, ei: EntityInstall, adapter: Any, doc: Any, ms: MappingSet) -> MappingSet:
    """Zoho Books: create the customer on first push (``masters_sync``) unless mapped or linked."""
    if ei.connector_key == "tally_xml" or getattr(doc, "client_id", None) is None:
        return ms
    plain = str(doc.client_id)
    profile = getattr(doc, "billing_profile_id", None)
    if ms.get("customer", plain, wildcard=False) or (profile and ms.get("customer", f"{plain}:{profile}", wildcard=False)):
        return ms
    if await integrations_service.find_link(db, ei.installation_id, "client", local_id=doc.client_id) is not None:
        return ms
    snap = dict(getattr(doc, "recipient_snapshot", None) or {})
    client = await db.get(Client, doc.client_id)
    name = str(snap.get("legal_name") or snap.get("name") or (client.name if client else "") or "")
    if not name:
        return ms
    country = str(snap.get("country") or "IN")[:2].upper()
    customer = {"key": plain, "name": name, "gstin": snap.get("gstin") or (client.gstin if client else None), "state_code": snap.get("state_code"), "address": snap.get("address") or {}, "foreign": country != "IN"}
    result = await adapter.masters_sync([customer], [])
    ref = dict(result).get(plain)
    if ref:
        await save_mapping(db, ei.installation_id, doc.legal_entity_id, "customer", plain, str(ref), name)
        await integrations_service.link(db, ei.installation_id, "client", doc.client_id, str(ref))
    return await mapping_set(db, ei.installation_id, doc.legal_entity_id)


async def push_one(ctx: RequestContext, db: AsyncSession, ei: EntityInstall, adapter: Any, kind: str, doc: Any, ms: MappingSet) -> SyncResult:
    rec = await ensure_record(db, ei.installation_id, kind, doc)
    if rec.status == "skipped":
        return SyncResult(skipped=1)
    version = rec.document_version
    if rec.synced_version >= version and rec.status == "synced":
        return SyncResult(skipped=1)  # same version already in the ledger: no-op
    if kind == "payment":
        echo = await integrations_service.find_link(db, ei.installation_id, "payment", local_id=doc.id)
        if echo is not None:  # the echo rule: a payment confirmed from the ledger is never pushed back
            rec.external_ref, rec.synced_version, rec.synced_at = echo.external_id, version, now()
            set_status(db, rec, "synced", message="Recorded from the ledger (not pushed back)")
            await db.flush()
            return SyncResult(skipped=1)
    if not await _period_open(db, rec.legal_entity_id, rec.doc_date):
        set_status(db, rec, "blocked_period", message="Blocked: period closed")
        await db.flush()
        return SyncResult(processed=1, skipped=1)
    if kind != "payment" and doc.status == "cancelled" and rec.external_ref is None:
        rec.synced_version = version
        set_status(db, rec, "skipped", message="Cancelled before it reached the ledger")
        rec.skip_reason = "cancelled_before_sync"
        await db.flush()
        return SyncResult(skipped=1)
    action = "create"
    if rec.external_ref is not None:
        action = "void" if (kind != "payment" and documents.doc_version(kind, doc) > 1) else "update"
    try:
        ms = await _ensure_customer(ctx, db, ei, adapter, doc, ms)
        built = await documents.build(db, ei.installation_id, kind, doc, ms, version=version, action=action, external_ref=rec.external_ref)
        if built.dto is None:
            await _missing(db, rec, ei, built.missing)
            await db.flush()
            return SyncResult(processed=1, failed=1, errors=(SyncError(kind, doc.id, None, "mapping_missing", rec.message or "Missing mapping"),))
        results = await adapter.push_documents([built.dto])
    except ConnectorPermanentError as exc:
        set_status(db, rec, "failed", message=str(exc))
        await db.flush()
        return SyncResult(processed=1, failed=1, errors=(SyncError(kind, doc.id, rec.external_ref, exc.code, sanitise(str(exc)) or exc.code),))
    except ManualActionRequired as exc:
        set_status(db, rec, "pending", message=exc.instructions)
        await db.flush()
        return SyncResult(processed=1, skipped=1)
    result = results[0] if results else None
    if result is None or result.status == "failed":
        set_status(db, rec, "failed", message=(result.message if result else None) or "The ledger refused the document")
        await db.flush()
        return SyncResult(processed=1, failed=1)
    if result.external_ref:
        rec.external_ref = result.external_ref[:200]
        await integrations_service.link(db, ei.installation_id, kind, doc.id, rec.external_ref, local_version=str(version))
    rec.external_number = str(getattr(doc, "number", None) or getattr(doc, "human_id", ""))[:60] or None
    rec.synced_version, rec.synced_at = version, now()
    set_status(db, rec, "synced", message=result.message)
    await db.flush()
    return SyncResult(processed=1, created=1 if action == "create" else 0, updated=0 if action == "create" else 1)


async def push_document_handler(ctx: RequestContext, db: AsyncSession, run: Any) -> SyncResult:
    if run.subject is None:
        return SyncResult()
    kind, doc_id = run.subject
    if kind not in ("invoice", "credit_note", "debit_note", "payment"):
        return SyncResult()
    doc = await documents.load_document(db, kind, doc_id)
    if doc is None or not _pushable(kind, doc):
        return SyncResult(skipped=1)  # drafts never push
    ei = await entity_install(db, run.installation_id, doc.legal_entity_id)
    if ei is None:
        return SyncResult(skipped=1)
    settings = await get_settings_row(db)
    if kind == "payment" and not settings.push_payments:
        return SyncResult(skipped=1)
    if not settings.auto_push_on_issue:
        await ensure_record(db, ei.installation_id, kind, doc)
        await db.flush()
        return SyncResult(skipped=1)
    adapter = run.adapter if run.config_id == ei.config_id else await integrations_service.adapter_for(ctx, db, run.installation_id, config_id=ei.config_id)
    ms = await mapping_set(db, ei.installation_id, doc.legal_entity_id)
    return await push_one(ctx, db, ei, adapter, kind, doc, ms)


async def discover(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID, from_date: date | None, to_date: date | None, *, payments: bool) -> int:
    """Create ``pending`` records for issued documents of the range that have none yet (issued before the install or
    while auto-push was off). Returns the number created."""
    created = 0
    for kind, model in _MODELS.items():
        model = cast("type[Invoice]", model)
        stmt = select(model).where(model.legal_entity_id == legal_entity_id, model.status.in_(("issued", "cancelled")), model.number.is_not(None))
        if from_date:
            stmt = stmt.where(model.doc_date >= from_date)
        if to_date:
            stmt = stmt.where(model.doc_date <= to_date)
        have = select(LedgerSyncRecord.document_id).where(LedgerSyncRecord.installation_id == installation_id, LedgerSyncRecord.document_kind == kind)
        for doc in (await db.execute(stmt.where(model.id.not_in(have)).limit(500))).scalars():
            if kind != "payment" and doc.status == "cancelled":
                continue
            await ensure_record(db, installation_id, kind, doc)
            created += 1
    if payments:
        stmt = select(Payment).where(Payment.legal_entity_id == legal_entity_id, Payment.reversed_at.is_(None))
        if from_date:
            stmt = stmt.where(Payment.payment_date >= from_date)
        if to_date:
            stmt = stmt.where(Payment.payment_date <= to_date)
        have = select(LedgerSyncRecord.document_id).where(LedgerSyncRecord.installation_id == installation_id, LedgerSyncRecord.document_kind == "payment")
        for pay in (await db.execute(stmt.where(Payment.id.not_in(have)).limit(500))).scalars():
            await ensure_record(db, installation_id, "payment", pay)
            created += 1
    await db.flush()
    return created


async def push_records(ctx: RequestContext, db: AsyncSession, ei: EntityInstall, legal_entity_id: uuid.UUID, from_date: date | None, to_date: date | None, *, limit: int = 200) -> dict[str, int]:
    settings = await get_settings_row(db)
    await discover(db, ei.installation_id, legal_entity_id, from_date, to_date, payments=settings.push_payments)
    for kind, model in _MODELS.items():  # synced documents cancelled since -> out_of_sync (version bump)
        cancelled = (
            await db.execute(
                select(model).join(LedgerSyncRecord, LedgerSyncRecord.document_id == model.id).where(
                    LedgerSyncRecord.installation_id == ei.installation_id, LedgerSyncRecord.document_kind == kind,
                    LedgerSyncRecord.status == "synced", LedgerSyncRecord.document_version < 2, model.status == "cancelled",
                )
            )
        ).scalars()  # fmt: skip
        for doc in list(cancelled):
            await ensure_record(db, ei.installation_id, kind, doc)
    await db.flush()
    stmt = select(LedgerSyncRecord).where(
        LedgerSyncRecord.installation_id == ei.installation_id, LedgerSyncRecord.legal_entity_id == legal_entity_id,
        LedgerSyncRecord.status.in_((*PUSHABLE, "blocked_period")),
    )  # fmt: skip
    if from_date:
        stmt = stmt.where(LedgerSyncRecord.doc_date >= from_date)
    if to_date:
        stmt = stmt.where(LedgerSyncRecord.doc_date <= to_date)
    order = {"invoice": 0, "debit_note": 1, "credit_note": 2, "payment": 3}
    recs = sorted((await db.execute(stmt.limit(limit))).scalars(), key=lambda r: (order.get(r.document_kind, 9), r.doc_date))
    adapter = await integrations_service.adapter_for(ctx, db, ei.installation_id, config_id=ei.config_id)
    ms = await mapping_set(db, ei.installation_id, legal_entity_id)
    counts = {"documents": len(recs), "synced": 0, "failed": 0, "skipped": 0, "blocked": 0}
    for rec in recs:
        doc = await documents.load_document(db, rec.document_kind, rec.document_id)
        if doc is None or not _pushable(rec.document_kind, doc):
            counts["skipped"] += 1
            continue
        await push_one(ctx, db, ei, adapter, rec.document_kind, doc, ms)
        ms = await mapping_set(db, ei.installation_id, legal_entity_id)
        await db.refresh(rec)
        key = {"synced": "synced", "failed": "failed", "blocked_period": "blocked"}.get(rec.status, "skipped")
        counts[key] += 1
    return counts
