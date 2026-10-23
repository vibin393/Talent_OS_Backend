"""Vendor Payable Statements (Epic 17 A8, FR-98; payroll sub-domain, AD-23 / AD-20 / AD-21).
* ``build_statement`` pulls the approved, unposted payable items of the vendor's ``payee_kind='vendor'``
  placements of one Legal Entity (``postings.load_unposted``, ledger ``payable``) and prices them:
  ``quantity x vendor_rate x pay multiplier[category]`` rounded per line (``core.money.line_taxable``);
  expense
  lines at their approved amount (reimbursable only); signed adjustment sources become ``adjustment`` lines.
  A draft creates **no postings**; re-building a draft replaces its lines.
* ``finalize_statement`` re-reads the pending items and refuses when the set changed (409
  ``statement_stale``),
  then posts every line (``postings.record_posted``; the partial unique ledger index guarantees an item is
  stated once, and ``payee_kind`` keeps it out of payroll), writes ``content_hash``, renders the PDF + CSV,
  and
  marks expense claims *Stated* (``expenses_service.mark_stated`` when Epic 27 has landed). A finalized
  statement is immutable (DB trigger -> 409 ``document_immutable``); corrections are new source rows.
* Rates: ``placements_service.pay_context`` / ``time_service.pay_inputs`` (Epics 19 / 25); until those land
  the
  rate segment's ``vendor_rate`` and a multiplier of 1 are used.
"""

import csv
import hashlib
import io
import json
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.money import line_taxable, quantize_amount
from app.core.sequences import next_human_id
from app.models.candidates import Candidate
from app.models.placements import Placement, RateSegment
from app.models.vendor_statements import VendorPayableStatement, VendorStatementLine
from app.models.vendors import Vendor
from app.schemas.vendors import (
    StatementBuildIn,
    StatementBuildOut,
    StatementDetailOut,
    StatementLineOut,
    StatementOut,
)
from app.services import files_service, postings

TARGET_KIND = "vendor_statement_line"
FILE_OWNER = "vendor_statement"
EXPENSE_KINDS = frozenset({"expense_line"})
ADJUSTMENT_KINDS = frozenset({"adjustment_entry", "rate_adjustment"})


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _user(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return getattr(p, "id", None) if getattr(p, "type", None) == "user" else None


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


# ================================================================ read models
def _out(s: VendorPayableStatement, *, vendor_name: str | None = None, line_count: int = 0) -> StatementOut:
    return StatementOut(
        id=s.id,
        human_id=s.human_id,
        vendor_id=s.vendor_id,
        vendor_name=vendor_name,
        legal_entity_id=s.legal_entity_id,
        period_start=s.period_start,
        period_end=s.period_end,
        status=s.status,
        currency=s.currency,
        total_amount=s.total_amount,
        content_hash=s.content_hash,
        finalized_at=s.finalized_at,
        line_count=line_count,
        created_at=s.created_at,
    )


def _line_out(ln: VendorStatementLine) -> StatementLineOut:
    return StatementLineOut(
        id=ln.id,
        line_kind=ln.line_kind,
        source_kind=ln.source_kind,
        source_id=ln.source_id,
        placement_id=ln.placement_id,
        worker_name_snapshot=ln.worker_name_snapshot,
        work_date=ln.work_date,
        time_category=ln.time_category,
        quantity=ln.quantity,
        vendor_rate=ln.vendor_rate,
        multiplier=ln.multiplier,
        amount=ln.amount,
        currency=ln.currency,
    )


async def _lines(db: AsyncSession, statement_id: uuid.UUID) -> list[VendorStatementLine]:
    return list(
        (
            await db.execute(
                select(VendorStatementLine)
                .where(VendorStatementLine.statement_id == statement_id)
                .order_by(
                    VendorStatementLine.worker_name_snapshot,
                    VendorStatementLine.work_date,
                    VendorStatementLine.id,
                )
            )
        ).scalars()
    )


async def _detail(db: AsyncSession, s: VendorPayableStatement) -> StatementDetailOut:
    lines = await _lines(db, s.id)
    name = (await db.execute(select(Vendor.legal_name).where(Vendor.id == s.vendor_id))).scalar_one_or_none()
    return StatementDetailOut(
        **_out(s, vendor_name=name, line_count=len(lines)).model_dump(), lines=[_line_out(ln) for ln in lines]
    )


async def _load(db: AsyncSession, statement_id: uuid.UUID, *, lock: bool = False) -> VendorPayableStatement:
    stmt = select(VendorPayableStatement).where(VendorPayableStatement.id == statement_id)
    if lock:
        stmt = stmt.with_for_update()
    s = (await db.execute(stmt)).scalar_one_or_none()
    if s is None:
        raise NotFound("Statement not found")
    return s


async def list_statements(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    vendor_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[StatementOut]:
    stmt = select(VendorPayableStatement, Vendor.legal_name).join(
        Vendor, Vendor.id == VendorPayableStatement.vendor_id
    )
    if vendor_id:
        stmt = stmt.where(VendorPayableStatement.vendor_id == vendor_id)
    if status:
        stmt = stmt.where(VendorPayableStatement.status == status)
    rows = (
        await db.execute(
            stmt.order_by(VendorPayableStatement.period_end.desc(), VendorPayableStatement.id).limit(500)
        )
    ).all()
    counts = dict(
        (
            await db.execute(
                select(VendorStatementLine.statement_id, func.count())
                .where(VendorStatementLine.statement_id.in_([s.id for s, _ in rows] or [uuid.uuid4()]))
                .group_by(VendorStatementLine.statement_id)
            )
        )
        .tuples()
        .all()
    )
    return [_out(s, vendor_name=name, line_count=int(counts.get(s.id, 0))) for s, name in rows]


async def get_statement(ctx: RequestContext, db: AsyncSession, statement_id: uuid.UUID) -> StatementDetailOut:  # ruff: ignore[unused-function-argument]
    return await _detail(db, await _load(db, statement_id))


# ================================================================ pricing
async def _vendor_placements(
    db: AsyncSession, vendor_id: uuid.UUID, legal_entity_id: uuid.UUID
) -> dict[uuid.UUID, Placement]:
    rows = (
        await db.execute(
            select(Placement).where(
                Placement.vendor_id == vendor_id,
                Placement.payee_kind == "vendor",
                Placement.legal_entity_id == legal_entity_id,
            )
        )
    ).scalars()
    return {p.id: p for p in rows}


async def _items(
    ctx: RequestContext,
    db: AsyncSession,
    placements: dict[uuid.UUID, Placement],
    legal_entity_id: uuid.UUID,
    start: date,
    end: date,
) -> list[postings.SourceItem]:
    if not placements:
        return []
    loaded = await postings.load_unposted(
        ctx,
        db,
        postings.SourceQuery(
            ledger="payable",
            period_start=start,
            period_end=end,
            placement_ids=list(placements),
            legal_entity_id=legal_entity_id,
            payee_kind="vendor",
        ),
    )
    out = []
    for item in loaded:
        if item.placement_id not in placements or item.item_date > end:
            continue
        if item.source_kind in EXPENSE_KINDS and not item.attributes.get("reimbursable", False):
            continue
        out.append(item)
    return out


async def _vendor_rate(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, on: date
) -> Decimal | None:
    from app.services import placements_service

    try:
        pc = await placements_service.pay_context(ctx, db, placement_id, on)
        return Decimal(pc.vendor_rate) if pc.vendor_rate is not None else None
    except NotImplementedError:
        seg = (
            await db.execute(
                select(RateSegment.vendor_rate)
                .where(
                    RateSegment.placement_id == placement_id,
                    RateSegment.effective_from <= on,
                    (RateSegment.effective_to.is_(None)) | (RateSegment.effective_to >= on),
                )
                .order_by(RateSegment.effective_from.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return Decimal(seg) if seg is not None else None


async def _multipliers(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, start: date, end: date
) -> dict[str, Decimal]:
    from app.services import time_service

    try:
        inputs = await time_service.pay_inputs(ctx, db, placement_id, start, end)
        return {k: Decimal(v) for k, v in inputs.pay_multipliers.items()}
    except NotImplementedError:
        return {}


def _price(
    item: postings.SourceItem, rate: Decimal | None, multiplier: Decimal, currency: str
) -> tuple[str, Decimal, Decimal | None]:
    """``(line_kind, amount, rate)`` for one source item (Decimal throughout, rounded per line)."""
    if item.source_kind in EXPENSE_KINDS:
        return "expense", quantize_amount(Decimal(item.amount or 0), currency), None
    if item.source_kind in ADJUSTMENT_KINDS:
        if item.amount is not None:
            return "adjustment", quantize_amount(Decimal(item.amount), currency), None
        delta = Decimal(str(item.attributes.get("rate_delta") or 0))
        return "adjustment", line_taxable(Decimal(item.quantity), delta, Decimal(1), currency), delta
    if rate is None:
        if item.amount is not None:
            return "time", quantize_amount(Decimal(item.amount), currency), None
        raise ValidationFailed(
            "A placement has no vendor rate",
            code="vendor_rate_missing",
            details={"placement_id": str(item.placement_id)},
        )
    return "time", line_taxable(Decimal(item.quantity), rate, multiplier, currency), rate


async def _worker_names(db: AsyncSession, placements: dict[uuid.UUID, Placement]) -> dict[uuid.UUID, str]:
    cand_ids = {p.candidate_id for p in placements.values()}
    names = (
        dict(
            (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cand_ids))))
            .tuples()
            .all()
        )
        if cand_ids
        else {}
    )
    return {pid: names.get(p.candidate_id, "") for pid, p in placements.items()}


async def build_statement(
    ctx: RequestContext,
    db: AsyncSession,
    vendor_id: uuid.UUID,
    legal_entity_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> StatementOut | None:
    """Draft (re)build for one vendor; ``None`` when there is nothing to state and no draft exists."""
    if period_end < period_start:
        raise ValidationFailed("The period end is before its start", code="invalid_period")
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()
    if vendor is None:
        raise NotFound("Vendor not found")
    placements = await _vendor_placements(db, vendor_id, legal_entity_id)
    items = await _items(ctx, db, placements, legal_entity_id, period_start, period_end)
    draft = (
        await db.execute(
            select(VendorPayableStatement)
            .where(
                VendorPayableStatement.vendor_id == vendor_id,
                VendorPayableStatement.legal_entity_id == legal_entity_id,
                VendorPayableStatement.period_start == period_start,
                VendorPayableStatement.period_end == period_end,
                VendorPayableStatement.status == "draft",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not items and draft is None:
        return None
    currency = next((p.currency for p in placements.values()), "INR")
    if draft is None:
        draft = VendorPayableStatement(
            id=uuid.uuid4(),
            tenant_id=_tenant(db),
            human_id=await next_human_id(db, "VPS", width=6),
            vendor_id=vendor_id,
            legal_entity_id=legal_entity_id,
            period_start=period_start,
            period_end=period_end,
            status="draft",
            currency=currency,
            built_by=_user(ctx),
        )
        db.add(draft)
        await db.flush()
    else:
        await db.execute(delete(VendorStatementLine).where(VendorStatementLine.statement_id == draft.id))
    names = await _worker_names(db, placements)
    mult_cache: dict[uuid.UUID, dict[str, Decimal]] = {}
    total = Decimal(0)
    for item in sorted(
        items, key=lambda i: (str(i.placement_id), i.item_date, i.source_kind, str(i.source_id))
    ):
        pid = item.placement_id
        assert pid is not None
        if pid not in mult_cache:
            mult_cache[pid] = await _multipliers(ctx, db, pid, period_start, period_end)
        category = item.attributes.get("category")
        multiplier = mult_cache[pid].get(str(category), Decimal(1)) if category else Decimal(1)
        rate = None
        if item.source_kind not in EXPENSE_KINDS | ADJUSTMENT_KINDS:
            rate = await _vendor_rate(ctx, db, pid, item.item_date)
        kind, amount, line_rate = _price(item, rate, multiplier, item.currency or currency)
        total += amount
        db.add(
            VendorStatementLine(
                id=uuid.uuid4(),
                tenant_id=draft.tenant_id,
                statement_id=draft.id,
                vendor_id=vendor_id,
                line_kind=kind,
                source_kind=item.source_kind,
                source_id=item.source_id,
                placement_id=pid,
                worker_name_snapshot=names.get(pid) or None,
                work_date=item.item_date,
                time_category=str(category) if category else None,
                quantity=Decimal(item.quantity),
                vendor_rate=line_rate,
                multiplier=multiplier if kind == "time" else Decimal(1),
                amount=amount,
                currency=(item.currency or currency).upper(),
            )
        )
    draft.total_amount = quantize_amount(total, draft.currency)
    draft.built_by = _user(ctx)
    await db.flush()
    n = len(items)
    return _out(draft, vendor_name=vendor.legal_name, line_count=n)


async def build(ctx: RequestContext, db: AsyncSession, data: StatementBuildIn) -> StatementBuildOut:
    if not data.all and not data.vendor_ids:
        raise ValidationFailed("Choose vendors or all", code="vendors_required")
    if data.all:
        vendor_ids = list(
            (
                await db.execute(
                    select(Placement.vendor_id)
                    .where(
                        Placement.payee_kind == "vendor", Placement.legal_entity_id == data.legal_entity_id
                    )
                    .distinct()
                )
            ).scalars()
        )
    else:
        vendor_ids = list(dict.fromkeys(data.vendor_ids or []))
    out: list[StatementOut] = []
    skipped: list[dict[str, Any]] = []
    for vid in vendor_ids:
        if vid is None:
            continue
        built = await build_statement(ctx, db, vid, data.legal_entity_id, data.period_start, data.period_end)
        if built is None:
            skipped.append({"vendor_id": str(vid), "reason": "nothing_to_state"})
        else:
            out.append(built)
    return StatementBuildOut(statements=out, skipped=skipped)


# ================================================================ finalize
def _hash(lines: Sequence[VendorStatementLine], s: VendorPayableStatement) -> str:
    payload = {
        "statement": [
            str(s.vendor_id),
            str(s.legal_entity_id),
            s.period_start.isoformat(),
            s.period_end.isoformat(),
        ],
        "lines": sorted(
            [
                ln.source_kind,
                str(ln.source_id),
                str(ln.quantity),
                str(ln.vendor_rate),
                str(ln.multiplier),
                str(ln.amount),
            ]
            for ln in lines
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _csv(s: VendorPayableStatement, lines: Sequence[VendorStatementLine]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "statement",
            "worker",
            "date",
            "kind",
            "category",
            "quantity",
            "vendor_rate",
            "multiplier",
            "amount",
            "currency",
        ]
    )
    for ln in lines:
        w.writerow(
            [
                s.human_id,
                ln.worker_name_snapshot or "",
                ln.work_date.isoformat(),
                ln.line_kind,
                ln.time_category or "",
                str(ln.quantity),
                str(ln.vendor_rate or ""),
                str(ln.multiplier),
                str(ln.amount),
                ln.currency,
            ]
        )
    w.writerow([s.human_id, "TOTAL", "", "", "", "", "", "", str(s.total_amount), s.currency])
    return buf.getvalue().encode("utf-8")


def _pdf(s: VendorPayableStatement, vendor_name: str, lines: Sequence[VendorStatementLine]) -> bytes:
    from app.services.documents.render import render_markdown

    rows = "\n".join(
        f"| {ln.worker_name_snapshot or ''} | {ln.work_date.isoformat()} | {ln.line_kind} | "
        f"{ln.time_category or ''} | "
        f"{ln.quantity} | {ln.vendor_rate or ''} | {ln.multiplier} | {ln.amount} |"
        for ln in lines
    )
    md = (
        f"# Vendor payable statement {s.human_id}\n\n"
        f"**Vendor:** {vendor_name}  \n**Period:** {s.period_start.isoformat()} to "
        f"{s.period_end.isoformat()}  \n"
        f"**Currency:** {s.currency}\n\n"
        "| Worker | Date | Kind | Category | Qty | Rate | Mult. | Amount "
        "|\n|---|---|---|---|---|---|---|---|\n"
        f"{rows}\n\n**Total:** {s.total_amount} {s.currency}\n"
    )
    return render_markdown(
        md, header=f"Statement {s.human_id}", footer=s.human_id, title=f"Statement {s.human_id}"
    )


async def _store(
    ctx: RequestContext,
    db: AsyncSession,
    s: VendorPayableStatement,
    data: bytes,
    filename: str,
    content_type: str,
) -> uuid.UUID:
    task_ctx = ctx if ctx.tenant_id is not None else RequestContext(tenant_id=s.tenant_id)
    f = await files_service.store_generated(
        task_ctx,
        db,
        data,
        key=storage.new_key(s.tenant_id),
        filename=filename,
        content_type=content_type,
        owner_entity_type=FILE_OWNER,
        owner_entity_id=s.id,
    )
    return f.id


async def _mark_expenses_stated(
    ctx: RequestContext, db: AsyncSession, line_ids: list[uuid.UUID], statement_id: uuid.UUID
) -> None:
    """Epic 27 contract (downward call inside the finalize transaction); skipped until expenses lands."""
    try:
        from app.services import expenses_service
    except ImportError:
        return
    mark = getattr(expenses_service, "mark_stated", None)
    if mark is None or not line_ids:
        return
    await mark(ctx, db, line_ids, statement_id)


async def finalize_statement(
    ctx: RequestContext, db: AsyncSession, statement_id: uuid.UUID
) -> StatementDetailOut:
    from app.core.events import emit
    from app.events.payroll import VendorStatementGenerated

    s = await _load(db, statement_id, lock=True)
    if s.status != "draft":
        raise ConflictError("Only a draft statement can be finalized", code="statement_not_draft")
    lines = await _lines(db, s.id)
    if not lines:
        raise ConflictError("The statement has no lines", code="statement_empty")
    placements = await _vendor_placements(db, s.vendor_id, s.legal_entity_id)
    pending = await _items(ctx, db, placements, s.legal_entity_id, s.period_start, s.period_end)
    if {(i.source_kind, i.source_id) for i in pending} != {(ln.source_kind, ln.source_id) for ln in lines}:
        raise ConflictError(
            "The approved items changed since the build; rebuild the statement", code="statement_stale"
        )
    await postings.record_posted(
        ctx,
        db,
        "payable",
        [
            postings.PostingLine(
                source_kind=ln.source_kind,
                source_id=ln.source_id,
                target_kind=TARGET_KIND,
                target_id=ln.id,
                document_id=s.id,
                quantity=Decimal(ln.quantity),
                amount=Decimal(ln.amount),
                currency=ln.currency,
                placement_id=ln.placement_id,
                payee_kind="vendor",
            )
            for ln in lines
        ],
    )
    vendor_name = (await db.execute(select(Vendor.legal_name).where(Vendor.id == s.vendor_id))).scalar_one()
    pdf_id = await _store(ctx, db, s, _pdf(s, vendor_name, lines), f"{s.human_id}.pdf", "application/pdf")
    csv_id = await _store(ctx, db, s, _csv(s, lines), f"{s.human_id}.csv", "text/csv")
    await _mark_expenses_stated(ctx, db, [ln.source_id for ln in lines if ln.line_kind == "expense"], s.id)
    s.content_hash = _hash(lines, s)
    s.file_id, s.csv_file_id = pdf_id, csv_id
    s.finalized_by, s.finalized_at = _user(ctx), _now()
    s.status = "finalized"
    await db.flush()
    pay_period_id = await _pay_period(ctx, db, s)
    if pay_period_id is not None:
        s.pay_period_id = pay_period_id
        emit(
            db,
            VendorStatementGenerated(
                tenant_id=s.tenant_id, statement_id=s.id, vendor_id=s.vendor_id, pay_period_id=pay_period_id
            ),
        )
    else:
        logger.info("Vendor statement {} finalized without a pay period (Epic 28 not landed)", s.id)
    return await _detail(db, s)


async def _pay_period(ctx: RequestContext, db: AsyncSession, s: VendorPayableStatement) -> uuid.UUID | None:
    from app.services.payroll import service as payroll_service

    try:
        ref = await payroll_service.pay_period_for(ctx, db, s.legal_entity_id, s.period_end)
    except NotImplementedError:
        return None
    return ref.id if ref is not None else None


async def delete_draft(ctx: RequestContext, db: AsyncSession, statement_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    s = await _load(db, statement_id, lock=True)
    if s.status != "draft":
        raise ConflictError("Only a draft statement can be deleted", code="document_immutable")
    await db.execute(delete(VendorStatementLine).where(VendorStatementLine.statement_id == s.id))
    await db.delete(s)
    await db.flush()


async def download(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    statement_id: uuid.UUID,
    fmt: str,
) -> files_service.Download:
    s = await _load(db, statement_id)
    if s.status == "draft":
        lines = await _lines(db, s.id)
        if fmt == "csv":
            return files_service.Download(
                data=_csv(s, lines), filename=f"{s.human_id}-draft.csv", content_type="text/csv"
            )
        name = (await db.execute(select(Vendor.legal_name).where(Vendor.id == s.vendor_id))).scalar_one()
        return files_service.Download(
            data=_pdf(s, name, lines), filename=f"{s.human_id}-draft.pdf", content_type="application/pdf"
        )
    file_id = s.csv_file_id if fmt == "csv" else s.file_id
    from app.models.files import StoredFile

    f = await db.get(StoredFile, file_id) if file_id else None
    if f is None or f.deleted_at is not None:
        raise NotFound("File not found")
    return files_service.Download(
        data=await files_service.read_file(f), filename=f.original_name, content_type=f.content_type
    )


async def file_access(ctx: RequestContext, db: AsyncSession, statement_id: uuid.UUID, write: bool) -> bool:
    """``files_service`` checker ``vendor_statement``: internal ``payroll:read``; the vendor's own users
    once final."""
    from app.core.permissions import Principal

    p = ctx.principal
    if p is None:
        return True
    if not isinstance(p, Principal) or write:
        return False
    s = (
        await db.execute(select(VendorPayableStatement).where(VendorPayableStatement.id == statement_id))
    ).scalar_one_or_none()
    if s is None:
        return False
    if p.type == "vendor_user":
        return (
            p.org_id == s.vendor_id and s.status == "finalized" and "statements:view" in p.portal_permissions
        )
    return p.type == "user" and p.scope_of("payroll:read") is not None
