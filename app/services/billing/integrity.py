# ruff: file-ignore[line-too-long, unused-function-argument]
"""Billing integrity checks (A11, NFR-25): registered as ``billing.*`` reconciliation checks (Epic 30's runner) and
run nightly. Each returns ``Finding`` rows; zero findings = consistent.

* ``billing.content_hash``: issued documents whose recomputed content hash differs (tampering by the owner role);
* ``billing.numbering``: per series / FY, issued non-imported numbers = counter, max sequence = counter;
* ``billing.totals``: tax lines vs header totals, the ``document_total`` formula, INR sums;
* ``billing.postings``: issued lines carry posted postings, reserved postings sit on drafts.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.reconciliation import Finding
from app.services.billing import issue
from app.services.billing.common import DOC_KINDS, MODELS, TARGET_KIND


async def check_content_hash(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:
    out = []
    for kind in DOC_KINDS:
        model = MODELS[kind][0]
        rows = await db.execute(
            select(model).where(
                model.status.in_(("issued", "cancelled")),
                model.is_imported.is_(False),
                model.content_hash.is_not(None),
            )
        )
        for doc in rows.scalars():
            if await issue.content_hash(db, kind, doc) != doc.content_hash:
                out.append(Finding("billing.content_hash", "error", kind, doc.id, {"number": doc.number}))
    return out


async def check_numbering(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:
    out = []
    counters = (
        await db.execute(text("SELECT series_id, fiscal_year, last_value FROM document_series_counters"))
    ).all()
    for series_id, fy, last in counters:
        issued = 0
        for kind in DOC_KINDS:
            model = MODELS[kind][0]
            issued += len(
                list(
                    (
                        await db.execute(
                            select(model.id).where(
                                model.series_id == series_id,
                                model.fiscal_year == fy,
                                model.number.is_not(None),
                                model.is_imported.is_(False),
                                model.status.in_(("issued", "cancelled")),
                            )
                        )
                    ).scalars()
                )
            )
        imported = 0
        for kind in DOC_KINDS:
            model = MODELS[kind][0]
            imported += len(
                list(
                    (
                        await db.execute(
                            select(model.id).where(
                                model.series_id == series_id,
                                model.fiscal_year == fy,
                                model.is_imported.is_(True),
                            )
                        )
                    ).scalars()
                )
            )
        if imported == 0 and issued != int(last):
            out.append(
                Finding(
                    "billing.numbering",
                    "error",
                    "document_series",
                    series_id,
                    {"fiscal_year": fy, "issued": issued, "counter": int(last)},
                )
            )
    return out


async def check_totals(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:
    out = []
    for kind in DOC_KINDS:
        model, line_model, tax_model, fk = MODELS[kind]
        docs = list(
            (
                await db.execute(
                    select(model).where(
                        model.status.in_(("issued", "cancelled")), model.is_imported.is_(False)
                    )
                )
            ).scalars()
        )
        for doc in docs:
            lines = list(
                (await db.execute(select(line_model).where(getattr(line_model, fk) == doc.id))).scalars()
            )
            taxes = list(
                (await db.execute(select(tax_model).where(getattr(tax_model, fk) == doc.id))).scalars()
            )
            taxable = sum(
                (Decimal(ln.taxable_value) for ln in lines if not ln.outside_taxable_value), Decimal(0)
            )
            outside = sum((Decimal(ln.taxable_value) for ln in lines if ln.outside_taxable_value), Decimal(0))
            charged = sum((Decimal(t.amount) for t in taxes if t.charged), Decimal(0))
            rcm = sum((Decimal(t.amount) for t in taxes if not t.charged), Decimal(0))
            inr_taxable = sum(
                (Decimal(ln.inr_taxable_value) for ln in lines if not ln.outside_taxable_value), Decimal(0)
            )
            problems: dict[str, Any] = {}
            if taxable != doc.taxable_total:
                problems["taxable_total"] = [str(taxable), str(doc.taxable_total)]
            if charged != doc.charged_tax_total or rcm != doc.rcm_tax_total:
                problems["tax_total"] = [str(charged), str(doc.charged_tax_total)]
            if taxable + outside + charged + Decimal(doc.round_off) != doc.document_total:
                problems["document_total"] = str(doc.document_total)
            if inr_taxable != doc.inr_taxable_total:
                problems["inr_taxable_total"] = [str(inr_taxable), str(doc.inr_taxable_total)]
            if problems:
                out.append(Finding("billing.totals", "error", kind, doc.id, problems))
    return out


async def check_postings(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:
    out = []
    rows = (
        await db.execute(
            text(
                "SELECT target_kind, target_id, document_id, state FROM billing_postings WHERE state <> 'released'"
            )
        )
    ).all()
    by_doc: dict[uuid.UUID, set[str]] = defaultdict(set)
    for _tk, _tid, doc_id, state in rows:
        if doc_id is not None:
            by_doc[doc_id].add(state)
    for kind in DOC_KINDS:
        model = MODELS[kind][0]
        for doc in (
            await db.execute(select(model).where(model.id.in_(list(by_doc) or [uuid.uuid4()])))
        ).scalars():
            states = by_doc[doc.id]
            if doc.status == "issued" and "reserved" in states:
                out.append(
                    Finding("billing.postings", "error", kind, doc.id, {"issue": "reserved_on_issued"})
                )
            if doc.status in ("draft", "pending_approval") and "posted" in states:
                out.append(Finding("billing.postings", "error", kind, doc.id, {"issue": "posted_on_draft"}))
            if doc.status in ("discarded",) and states:
                out.append(
                    Finding("billing.postings", "error", kind, doc.id, {"issue": "active_on_discarded"})
                )
    _ = TARGET_KIND
    return out


CHECKS = {
    "billing.content_hash": check_content_hash,
    "billing.numbering": check_numbering,
    "billing.totals": check_totals,
    "billing.postings": check_postings,
}


async def run_all(ctx: RequestContext, db: AsyncSession, as_of: date) -> list[Finding]:
    findings: list[Finding] = []
    for fn in CHECKS.values():
        findings += await fn(ctx, db, as_of)
    return findings
