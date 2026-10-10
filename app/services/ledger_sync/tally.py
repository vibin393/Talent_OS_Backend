# ruff: noqa: E501
"""Tally import files (Epic 71, story 71.4, spec A7; D-5): one file per run, Legal Entity and period with the issued
invoices, notes and (``include_payments``) receipts of the period; optional party-ledger masters. Each voucher's
``REMOTEID`` is ``talentos-{document_id}-v{version}`` so re-generating a period gives the same ids (TallyPrime updates
instead of duplicating). Records become ``synced`` with ``tally_run_id``; a document with a missing mapping is
``failed`` (fix-it Task) and left out of the file; a document dated in a closed Accounting Period is
``blocked_period`` and left out (the same period guard as the push)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.context import RequestContext
from app.models.billing import CreditNote, DebitNote, Invoice
from app.models.ledger_sync import LedgerRun
from app.models.receivables import Payment
from app.services import files_service, integrations_service
from app.services.connectors.accounting.tally_xml import voucher_row
from app.services.ledger_sync import documents
from app.services.ledger_sync.common import EntityInstall, now
from app.services.ledger_sync.mappings import mapping_set
from app.services.ledger_sync.push import _missing, _period_open, ensure_record, set_status

__all__ = ["FILE_OWNER", "generate"]

FILE_OWNER = "ledger_run"
_MODELS: dict[str, Any] = {"invoice": Invoice, "debit_note": DebitNote, "credit_note": CreditNote}


async def generate(ctx: RequestContext, db: AsyncSession, ei: EntityInstall, run: LedgerRun) -> None:
    assert run.from_date is not None and run.to_date is not None
    adapter = await integrations_service.adapter_for(ctx, db, ei.installation_id, config_id=ei.config_id)
    cfg = getattr(adapter, "ctx", None)
    include_payments = bool(getattr(getattr(cfg, "config", None), "include_payments", True))
    ms = await mapping_set(db, ei.installation_id, run.legal_entity_id)
    docs: list[tuple[str, Any]] = []
    for kind, model in _MODELS.items():
        rows = (
            await db.execute(
                select(model).where(
                    model.legal_entity_id == run.legal_entity_id, model.status == "issued", model.number.is_not(None),
                    model.doc_date >= run.from_date, model.doc_date <= run.to_date,
                ).order_by(model.doc_date, model.number)
            )
        ).scalars()  # fmt: skip
        docs.extend((kind, d) for d in rows)
    if include_payments:
        pays = (
            await db.execute(
                select(Payment).where(
                    Payment.legal_entity_id == run.legal_entity_id, Payment.reversed_at.is_(None),
                    Payment.payment_date >= run.from_date, Payment.payment_date <= run.to_date,
                ).order_by(Payment.payment_date, Payment.human_id)
            )
        ).scalars()  # fmt: skip
        docs.extend(("payment", p) for p in pays)
    rows: list[dict[str, Any]] = []
    masters: dict[str, dict[str, Any]] = {}
    counts = {"documents": len(docs), "synced": 0, "failed": 0, "skipped": 0, "blocked": 0}
    built_recs = []
    for kind, doc in docs:
        rec = await ensure_record(db, ei.installation_id, kind, doc)
        if rec.status == "skipped":
            counts["skipped"] += 1
            continue
        if not await _period_open(db, rec.legal_entity_id, rec.doc_date):
            set_status(db, rec, "blocked_period", message="Blocked: period closed")
            counts["blocked"] += 1
            continue
        built = await documents.build(db, ei.installation_id, kind, doc, ms, version=rec.document_version, include_masters=run.include_masters)
        if built.dto is None:
            await _missing(db, rec, ei, built.missing)
            counts["failed"] += 1
            continue
        if built.master is not None:
            masters[built.master["name"]] = built.master
        rows.append(voucher_row(built.dto))
        built_recs.append((rec, built.dto))
    results = await adapter.push_documents([d for _, d in built_recs]) if built_recs else []
    by_doc = {r.document_id: r for r in results}
    for rec, dto in built_recs:
        res = by_doc.get(dto.document_id)
        rec.external_ref = (res.external_ref if res else None) or rec.external_ref
        rec.external_number = dto.number[:60]
        rec.tally_run_id, rec.synced_version, rec.synced_at = run.id, rec.document_version, now()
        set_status(db, rec, "synced", message=None)
        counts["synced"] += 1
    data = await adapter.export(rows, {"masters": list(masters.values()) if run.include_masters else []})
    filename = f"tally-{run.from_date:%Y%m%d}-{run.to_date:%Y%m%d}-{str(run.id)[:8]}.xml"
    stored = await files_service.store_generated(
        ctx, db, data, key=storage.new_key(run.tenant_id), filename=filename, content_type="application/xml",
        owner_entity_type=FILE_OWNER, owner_entity_id=run.id,
    )  # fmt: skip
    run.file_id, run.counts = stored.id, counts
    await db.flush()
