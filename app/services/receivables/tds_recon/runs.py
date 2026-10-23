# ruff: file-ignore[line-too-long]
"""26AS / AIS reconciliation runs, proposals, human decisions and TAN mappings (Epic 51, Story 51.2, FR-254).

* ``create_run`` (API): validates the Legal Entity PAN, FY and quarter; an upload is parsed once synchronously to
  refuse a wrong PAN / FY up front (422 ``tds_statement_pan_mismatch`` / ``tds_statement_fy_mismatch``), stored
  through ``core.storage`` + ``core.scanning`` as a P2 document (``access_level='finance'``, retention
  ``financial``), and the run is queued (task ``finance_p3.tds_parse`` on ``finance_runs``). Without a file, a
  configured non-Manual ``tds_statement`` connector is fetched instead (the port's ``tan`` argument carries the
  Legal Entity PAN: 26AS / AIS are keyed by the deductee).
* ``process_run`` (task): stores the quarter's lines, supersedes the previous ready run of the same LE / FY /
  quarter (confirmed and acknowledged decisions carried over by ``line_hash``), proposes matches
  (``matching.propose``) and emits ``TdsReconRunCompleted``.
* Decisions (``confirm`` / ``reject`` / manual ``pair`` / ``acknowledge``) are human-only (FR-257,
  ``tds_recon_match: confirmed``) and audited. Reconciliation NEVER writes ``tds_deductions`` or
  ``tds_deduction_reversals``.
"""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.receivables import TdsReconRunCompleted
from app.models.receivables import (
    TdsDeduction,
    TdsDeductionReversal,
    TdsDeductorTan,
    TdsReconMatch,
    TdsReconRun,
    TdsStatementLine,
)
from app.schemas.tds_recon import (
    TdsCandidateOut,
    TdsDeductionRef,
    TdsDeductorTanIn,
    TdsDeductorTanOut,
    TdsLineOut,
    TdsMatchOut,
    TdsParseErrorOut,
    TdsReconRunDetailOut,
    TdsReconRunOut,
)
from app.services.receivables.tds_recon import matching, parser_26as, parser_ais
from app.services.receivables.tds_recon.common import (
    ParsedLine,
    ParsedStatement,
    StatementError,
    decode,
    fy_bounds,
    fy_from_assessment_year,
    fy_of,
    fy_short,
    hashes,
    quarter_of,
    valid_fy,
)

OPEN = ("proposed", "confirmed", "unmatched_ack")
CARRIED = ("confirmed", "unmatched_ack")
FILE_OWNER = "tds_recon_run"


@dataclasses.dataclass(frozen=True, slots=True)
class TdsParseQueued:
    """Private after-commit event: parse the run in the ``finance_runs`` queue once the upload has committed."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _user(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return getattr(p, "id", None) if getattr(p, "type", None) == "user" else None


# ---------------------------------------------------------------- shared lookups
async def legal_entity(db: AsyncSession, legal_entity_id: uuid.UUID) -> Any:
    from app.models.legal_entities import LegalEntity

    le = await db.get(LegalEntity, legal_entity_id)
    if le is None:
        raise NotFound("Legal entity not found")
    return le


async def tan_map(db: AsyncSession) -> dict[str, uuid.UUID]:
    return {r.tan: r.client_id for r in (await db.execute(select(TdsDeductorTan))).scalars()}


async def client_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.clients import Client

    if not ids:
        return {}
    rows = await db.execute(select(Client.id, Client.name).where(Client.id.in_(ids)))
    return {cid: str(name) for cid, name in rows.tuples()}


def reversed_ids_subquery() -> Any:
    return select(TdsDeductionReversal.tds_deduction_id)


async def fy_deductions(db: AsyncSession, legal_entity_id: uuid.UUID, fy: str) -> list[TdsDeduction]:
    """Income-tax TDS deductions of the LE dated in ``fy`` that were not reversed (read only)."""
    start, end = fy_bounds(fy)
    rows = await db.execute(
        select(TdsDeduction)
        .where(
            TdsDeduction.legal_entity_id == legal_entity_id,
            TdsDeduction.kind == "income_tax",
            TdsDeduction.deduction_date >= start,
            TdsDeduction.deduction_date <= end,
            TdsDeduction.id.not_in(reversed_ids_subquery()),
        )
        .order_by(TdsDeduction.deduction_date, TdsDeduction.id)
    )
    return list(rows.scalars())


async def confirmed_deduction_ids(db: AsyncSession) -> set[uuid.UUID]:
    rows = await db.execute(
        select(TdsReconMatch.tds_deduction_id).where(
            TdsReconMatch.active.is_(True), TdsReconMatch.status == "confirmed"
        )
    )
    return {r for r in rows.scalars() if r is not None}


def deduction_ref(d: TdsDeduction, names: dict[uuid.UUID, str]) -> TdsDeductionRef:
    return TdsDeductionRef(
        id=d.id,
        client_id=d.client_id,
        client_name=names.get(d.client_id),
        section_code=d.section_code,
        inr_amount=Decimal(d.inr_amount),
        deduction_date=d.deduction_date,
        quarter=d.quarter,
        invoice_id=d.invoice_id,
    )


# ---------------------------------------------------------------- parsing
def parse_statement(kind: str, data: bytes) -> ParsedStatement:
    text = decode(data)
    return parser_26as.parse(text) if kind == "26as" else parser_ais.parse(text)


def check_statement(parsed: ParsedStatement, pan: str, fy: str) -> None:
    if parsed.pan is None or parsed.pan.upper() != pan.upper():
        raise ValidationFailed(
            "The statement belongs to another PAN than the Legal Entity's", code="tds_statement_pan_mismatch"
        )
    stated_fy = fy_from_assessment_year(parsed.assessment_year)
    if stated_fy is not None and stated_fy != fy:
        raise ValidationFailed(
            f"The statement is for FY {stated_fy}, not {fy}",
            code="tds_statement_fy_mismatch",
            details={"statement_fy": stated_fy},
        )


def in_quarter(lines: tuple[ParsedLine, ...] | list[ParsedLine], fy: str, quarter: str) -> list[ParsedLine]:
    return [
        ln for ln in lines if fy_of(ln.transaction_date) == fy and quarter_of(ln.transaction_date) == quarter
    ]


# ---------------------------------------------------------------- create (API)
async def create_run(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID,
    fy: str,
    quarter: str,
    statement_kind: str,
    data: bytes | None,
    filename: str | None,
) -> TdsReconRunOut:
    from app.services.connectors import registry

    if not valid_fy(fy):
        raise ValidationFailed("Use a financial year like 2026-27", code="invalid_fy")
    le = await legal_entity(db, legal_entity_id)
    if not le.pan:
        raise ValidationFailed("Record the Legal Entity's PAN first", code="legal_entity_pan_missing")
    run = TdsReconRun(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        legal_entity_id=le.id,
        fy=fy,
        quarter=quarter,
        statement_kind=statement_kind,
        status="parsing",
        created_by=_user(ctx),
    )
    if data:
        try:
            parsed = parse_statement(statement_kind, data)
        except StatementError as exc:
            raise ValidationFailed(exc.message, code=exc.code) from exc
        check_statement(parsed, le.pan, fy)
        run.source = "upload"
        run.assessment_year = (parsed.assessment_year or "")[:9] or None
        db.add(run)
        await db.flush()
        run.file_id = await _store_file(ctx, db, run, data, filename)
    else:
        adapter = await registry.get(ctx, db, "tds_statement", le.id)
        if getattr(adapter, "provider_key", "manual") == "manual":
            raise ValidationFailed(
                "Upload the TRACES 26AS / AIS file (no statement connector is configured)",
                code="tds_statement_file_required",
            )
        run.source = "connector"
        db.add(run)
    await db.flush()
    audit.record(
        "tds_recon_run.created",
        "tds_recon_run",
        run.id,
        session=db,
        legal_entity_id=str(le.id),
        fy=fy,
        quarter=quarter,
        statement_kind=statement_kind,
        source=run.source,
    )
    emit(db, TdsParseQueued(tenant_id=run.tenant_id, run_id=run.id))
    return await run_out(db, run)


async def _store_file(
    ctx: RequestContext, db: AsyncSession, run: TdsReconRun, data: bytes, filename: str | None
) -> uuid.UUID:
    from app.core import storage
    from app.services import files_service
    from app.services.documents import repository

    files_service.check_size(data)
    document = await repository.create_document(
        ctx,
        db,
        owner_kind=FILE_OWNER,
        owner_id=run.id,
        category="tds_statement",
        title=f"{run.statement_kind.upper()} {run.fy} {run.quarter}",
        access_level="finance",
        retention_class="financial",
    )
    name = filename or f"{run.statement_kind}-{run.fy}-{run.quarter}.txt"
    content_type = "text/csv" if run.statement_kind == "ais" else "text/plain"
    stored = await files_service.store_generated(
        ctx,
        db,
        data,
        key=storage.new_key(run.tenant_id),
        filename=name,
        content_type=content_type,
        owner_entity_type=repository.FILE_OWNER,
        owner_entity_id=document.id,
    )
    await repository.append_version(ctx, db, document, stored, source="upload")
    return stored.id


# ---------------------------------------------------------------- process (task)
async def _statement_for(
    ctx: RequestContext, db: AsyncSession, run: TdsReconRun, pan: str
) -> ParsedStatement:
    from app.services import files_service
    from app.services.connectors import registry

    if run.source == "upload":
        f = await files_service.get_file(db, run.file_id) if run.file_id else None
        if f is None:
            raise StatementError("tds_statement_unreadable", "The uploaded statement file is missing")
        return parse_statement(run.statement_kind, await files_service.read_file(f))
    adapter: Any = await registry.get(ctx, db, "tds_statement", run.legal_entity_id)
    dto = await adapter.fetch(
        pan, run.fy, run.quarter
    )  # 26AS is keyed by the deductee PAN (frozen name ``tan``)
    lines = tuple(
        ParsedLine(
            line_no=i,
            line_ref=ln.line_ref[:100],
            deductor_tan=ln.deductor_tan.upper()[:10],
            deductor_name=ln.deductor_name[:200],
            section=ln.section[:10],
            transaction_date=ln.transaction_date,
            amount_paid=Decimal(ln.amount_paid),
            tds_deducted=Decimal(ln.tds_deducted),
            tds_deposited=None,
            booking_status=(ln.booking_status or "")[:1] or None,
        )
        for i, ln in enumerate(dto.lines, start=1)
    )
    return ParsedStatement(pan=pan, assessment_year=None, lines=lines)


async def process_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> str:
    run = (
        await db.execute(select(TdsReconRun).where(TdsReconRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None or run.status != "parsing":
        return run.status if run is not None else "missing"
    le = await legal_entity(db, run.legal_entity_id)
    try:
        parsed = await _statement_for(ctx, db, run, le.pan or "")
        if run.source == "upload":
            check_statement(parsed, le.pan or "", run.fy)
    except (StatementError, ValidationFailed) as exc:
        run.status = "failed"
        run.error = getattr(exc, "message", None) or str(exc)
        run.completed_at = _now()
        await db.flush()
        return "failed"
    lines = in_quarter(parsed.lines, run.fy, run.quarter)
    line_hashes = hashes(lines)
    tid = run.tenant_id
    rows: list[TdsStatementLine] = []
    for ln, h in zip(lines, line_hashes, strict=True):
        row = TdsStatementLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            run_id=run.id,
            line_no=ln.line_no,
            line_ref=ln.line_ref[:100],
            deductor_tan=ln.deductor_tan,
            deductor_name=ln.deductor_name,
            section=ln.section,
            transaction_date=ln.transaction_date,
            amount_paid=ln.amount_paid,
            tds_deducted=ln.tds_deducted,
            tds_deposited=ln.tds_deposited,
            booking_status=ln.booking_status,
            line_hash=h,
        )
        db.add(row)
        rows.append(row)
    run.parse_errors = [{"line_no": e.line_no, "message": e.message[:500]} for e in parsed.errors]
    run.line_count = len(rows)
    await db.flush()
    await _supersede(db, run, rows)
    await propose(db, run)
    run.status = "ready"
    run.completed_at = _now()
    await db.flush()
    emit(db, TdsReconRunCompleted(tenant_id=tid, run_id=run.id))
    return "ready"


async def _supersede(db: AsyncSession, run: TdsReconRun, new_rows: list[TdsStatementLine]) -> None:
    older = list(
        (
            await db.execute(
                select(TdsReconRun).where(
                    TdsReconRun.legal_entity_id == run.legal_entity_id,
                    TdsReconRun.fy == run.fy,
                    TdsReconRun.quarter == run.quarter,
                    TdsReconRun.status == "ready",
                    TdsReconRun.id != run.id,
                )
            )
        ).scalars()
    )
    if not older:
        return
    old_ids = [r.id for r in older]
    old_line_ids = select(TdsStatementLine.id).where(TdsStatementLine.run_id.in_(old_ids))
    carried = list(
        (
            await db.execute(
                select(TdsReconMatch, TdsStatementLine.line_hash)
                .join(TdsStatementLine, TdsStatementLine.id == TdsReconMatch.statement_line_id)
                .where(
                    TdsReconMatch.statement_line_id.in_(old_line_ids),
                    TdsReconMatch.active.is_(True),
                    TdsReconMatch.status.in_(CARRIED),
                )
            )
        ).tuples()
    )
    await db.execute(
        update(TdsReconMatch)
        .where(TdsReconMatch.statement_line_id.in_(old_line_ids), TdsReconMatch.active.is_(True))
        .values(active=False)
        .execution_options(synchronize_session=False)
    )
    for r in older:
        r.status = "superseded"
        r.superseded_by_id = run.id
    await db.flush()
    by_hash = {row.line_hash: row for row in new_rows}
    reversed_ids = set((await db.execute(reversed_ids_subquery())).scalars())
    for match, h in carried:
        target = by_hash.get(h)
        if target is None or (match.tds_deduction_id is not None and match.tds_deduction_id in reversed_ids):
            continue
        db.add(
            TdsReconMatch(
                id=uuid.uuid4(),
                tenant_id=run.tenant_id,
                statement_line_id=target.id,
                tds_deduction_id=match.tds_deduction_id,
                status=match.status,
                active=True,
                score=match.score,
                reasons=[*list(match.reasons or []), "carried_over"],
                decided_by=match.decided_by,
                decided_at=match.decided_at,
                decided_actor=match.decided_actor,
                note=match.note,
                carried_from_id=match.id,
            )
        )
    await db.flush()


async def propose(db: AsyncSession, run: TdsReconRun) -> int:
    """(Re)propose matches for the run's lines that have no open match. Deterministic; returns proposals made."""
    lines = list(
        (await db.execute(select(TdsStatementLine).where(TdsStatementLine.run_id == run.id))).scalars()
    )
    open_lines = set(
        (
            await db.execute(
                select(TdsReconMatch.statement_line_id).where(
                    TdsReconMatch.statement_line_id.in_([ln.id for ln in lines] or [uuid.uuid4()]),
                    TdsReconMatch.active.is_(True),
                    TdsReconMatch.status.in_(OPEN),
                )
            )
        ).scalars()
    )
    rejected = set(
        (
            await db.execute(
                select(TdsReconMatch.statement_line_id, TdsReconMatch.tds_deduction_id).where(
                    TdsReconMatch.statement_line_id.in_([ln.id for ln in lines] or [uuid.uuid4()]),
                    TdsReconMatch.active.is_(True),
                    TdsReconMatch.status == "rejected",
                )
            )
        ).tuples()
    )
    tans = await tan_map(db)
    taken = await confirmed_deduction_ids(db)
    taken |= set(
        (  # type: ignore[arg-type]
            await db.execute(
                select(TdsReconMatch.tds_deduction_id).where(
                    TdsReconMatch.active.is_(True), TdsReconMatch.status == "proposed"
                )
            )
        ).scalars()
    )
    candidates = [
        matching.DeductionIn(d.id, d.client_id, d.section_code, Decimal(d.inr_amount), d.deduction_date)
        for d in await fy_deductions(db, run.legal_entity_id, run.fy)
        if d.id not in taken
    ]
    line_in = [
        matching.LineIn(
            ln.id, tans.get(ln.deductor_tan), ln.section, ln.transaction_date, Decimal(ln.tds_deducted)
        )
        for ln in lines
        if ln.id not in open_lines
    ]
    made = 0
    for p in matching.propose(line_in, candidates):
        if (p.line_id, p.deduction_id) in rejected:
            continue  # a person already rejected exactly this pairing
        db.add(
            TdsReconMatch(
                id=uuid.uuid4(),
                tenant_id=run.tenant_id,
                statement_line_id=p.line_id,
                tds_deduction_id=p.deduction_id,
                status="proposed",
                score=p.score,
                reasons=list(p.reasons),
            )
        )
        made += 1
    await db.flush()
    return made


# ---------------------------------------------------------------- read
async def _counts(db: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    if not run_ids:
        return {}
    rows = await db.execute(
        select(TdsStatementLine.run_id, TdsReconMatch.status, func.count())
        .join(TdsReconMatch, TdsReconMatch.statement_line_id == TdsStatementLine.id)
        .where(TdsStatementLine.run_id.in_(run_ids), TdsReconMatch.active.is_(True))
        .group_by(TdsStatementLine.run_id, TdsReconMatch.status)
    )
    out: dict[uuid.UUID, dict[str, int]] = {}
    for run_id, st, n in rows.tuples():
        out.setdefault(run_id, {})[st] = int(n)
    return out


async def run_out(db: AsyncSession, run: TdsReconRun, counts: dict[str, int] | None = None) -> TdsReconRunOut:
    if counts is None:
        counts = (await _counts(db, [run.id])).get(run.id, {})
    decided = counts.get("proposed", 0) + counts.get("confirmed", 0) + counts.get("unmatched_ack", 0)
    return TdsReconRunOut(
        id=run.id,
        legal_entity_id=run.legal_entity_id,
        fy=run.fy,
        quarter=run.quarter,
        statement_kind=run.statement_kind,
        source=run.source,
        status=run.status,
        line_count=run.line_count,
        error=run.error,
        created_by=run.created_by,
        created_at=run.created_at,
        proposed=counts.get("proposed", 0),
        confirmed=counts.get("confirmed", 0),
        unmatched=max(run.line_count - decided, 0),
    )


async def list_runs(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID | None = None,
    fy: str | None = None,
    quarter: str | None = None,
) -> list[TdsReconRunOut]:
    stmt = select(TdsReconRun)
    if legal_entity_id:
        stmt = stmt.where(TdsReconRun.legal_entity_id == legal_entity_id)
    if fy:
        stmt = stmt.where(TdsReconRun.fy == fy)
    if quarter:
        stmt = stmt.where(TdsReconRun.quarter == quarter)
    runs = list((await db.execute(stmt.order_by(TdsReconRun.created_at.desc()).limit(200))).scalars())
    counts = await _counts(db, [r.id for r in runs])
    return [await run_out(db, r, counts.get(r.id, {})) for r in runs]


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> TdsReconRun:
    run = await db.get(TdsReconRun, run_id)
    if run is None:
        raise NotFound("Reconciliation run not found")
    return run


async def match_out(
    db: AsyncSession, m: TdsReconMatch, names: dict[uuid.UUID, str] | None = None
) -> TdsMatchOut:
    ded = await db.get(TdsDeduction, m.tds_deduction_id) if m.tds_deduction_id else None
    if names is None:
        names = await client_names(db, {ded.client_id} if ded else set())
    return TdsMatchOut(
        id=m.id,
        statement_line_id=m.statement_line_id,
        tds_deduction_id=m.tds_deduction_id,
        status=m.status,
        score=m.score,
        reasons=list(m.reasons or []),
        decided_by=m.decided_by,
        decided_at=m.decided_at,
        note=m.note,
        deduction=deduction_ref(ded, names) if ded else None,
    )


async def lines_out(db: AsyncSession, lines: list[TdsStatementLine]) -> list[TdsLineOut]:
    if not lines:
        return []
    tans = await tan_map(db)
    matches = list(
        (
            await db.execute(
                select(TdsReconMatch)
                .where(
                    TdsReconMatch.statement_line_id.in_([ln.id for ln in lines]),
                    TdsReconMatch.active.is_(True),
                )
                .order_by(TdsReconMatch.created_at)
            )
        ).scalars()
    )
    current: dict[uuid.UUID, TdsReconMatch] = {}
    for m in matches:  # an open match wins over an older rejected one
        prev = current.get(m.statement_line_id)
        if prev is None or m.status in OPEN or prev.status not in OPEN:
            current[m.statement_line_id] = m
    ded_ids = {m.tds_deduction_id for m in current.values() if m.tds_deduction_id}
    deds = {
        d.id: d
        for d in (
            await db.execute(select(TdsDeduction).where(TdsDeduction.id.in_(ded_ids or {uuid.uuid4()})))
        ).scalars()
    }
    names = await client_names(db, {d.client_id for d in deds.values()} | set(tans.values()))
    out: list[TdsLineOut] = []
    for ln in lines:
        m = current.get(ln.id)  # type: ignore[assignment]
        client_id = tans.get(ln.deductor_tan)
        mo = None
        if m is not None:
            d = deds.get(m.tds_deduction_id) if m.tds_deduction_id else None
            mo = TdsMatchOut(
                id=m.id,
                statement_line_id=m.statement_line_id,
                tds_deduction_id=m.tds_deduction_id,
                status=m.status,
                score=m.score,
                reasons=list(m.reasons or []),
                decided_by=m.decided_by,
                decided_at=m.decided_at,
                note=m.note,
                deduction=deduction_ref(d, names) if d else None,
            )
        out.append(
            TdsLineOut(
                id=ln.id,
                line_ref=ln.line_ref,
                deductor_tan=ln.deductor_tan,
                deductor_name=ln.deductor_name,
                section=ln.section,
                transaction_date=ln.transaction_date,
                amount_paid=Decimal(ln.amount_paid),
                tds_deducted=Decimal(ln.tds_deducted),
                tds_deposited=Decimal(ln.tds_deposited) if ln.tds_deposited is not None else None,
                booking_status=ln.booking_status,
                client_id=client_id,
                client_name=names.get(client_id) if client_id else None,
                match=mo,
            )
        )
    return out


async def run_detail(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> TdsReconRunDetailOut:  # ruff: ignore[unused-function-argument]
    run = await get_run(db, run_id)
    lines = list(
        (
            await db.execute(
                select(TdsStatementLine)
                .where(TdsStatementLine.run_id == run.id)
                .order_by(TdsStatementLine.line_no)
            )
        ).scalars()
    )
    return TdsReconRunDetailOut(
        run=await run_out(db, run),
        lines=await lines_out(db, lines),
        errors=[TdsParseErrorOut(**e) for e in (run.parse_errors or [])],
    )


async def candidates(ctx: RequestContext, db: AsyncSession, line_id: uuid.UUID) -> list[TdsCandidateOut]:  # ruff: ignore[unused-function-argument]
    line, run = await _line(db, line_id)
    taken = await confirmed_deduction_ids(db)
    tans = await tan_map(db)
    mapped = tans.get(line.deductor_tan)
    li = matching.LineIn(line.id, mapped, line.section, line.transaction_date, Decimal(line.tds_deducted))
    deds = [d for d in await fy_deductions(db, run.legal_entity_id, run.fy) if d.id not in taken]
    names = await client_names(db, {d.client_id for d in deds})
    scored = []
    for d in deds:
        s = matching.score(
            li,
            matching.DeductionIn(d.id, d.client_id, d.section_code, Decimal(d.inr_amount), d.deduction_date),
        )
        scored.append(
            (
                -(1 if mapped and d.client_id == mapped else 0),
                -s.value,
                d.deduction_date,
                str(d.id),
                d,
                s.value,
            )
        )
    scored.sort(key=lambda t: t[:4])
    return [TdsCandidateOut(**deduction_ref(d, names).model_dump(), score=v) for *_k, d, v in scored[:50]]


# ---------------------------------------------------------------- decisions (human-only, FR-257)
async def _line(db: AsyncSession, line_id: uuid.UUID) -> tuple[TdsStatementLine, TdsReconRun]:
    line = await db.get(TdsStatementLine, line_id)
    if line is None:
        raise NotFound("Statement line not found")
    run = await get_run(db, line.run_id)
    return line, run


def _assert_live(run: TdsReconRun) -> None:
    if run.status != "ready":
        raise ConflictError(
            "This run is not open for decisions", code="tds_run_not_ready", details={"status": run.status}
        )


async def _match(
    db: AsyncSession, match_id: uuid.UUID
) -> tuple[TdsReconMatch, TdsStatementLine, TdsReconRun]:
    m = (
        await db.execute(select(TdsReconMatch).where(TdsReconMatch.id == match_id).with_for_update())
    ).scalar_one_or_none()
    if m is None:
        raise NotFound("Match not found")
    line, run = await _line(db, m.statement_line_id)
    return m, line, run


def _decide(ctx: RequestContext, m: TdsReconMatch, status: str, note: str | None) -> None:
    actor = actor_from_ctx(ctx)
    m.status = status
    m.decided_by = _user(ctx)
    m.decided_at = _now()
    m.decided_actor = actor.to_json()
    if note:
        m.note = note


async def _flush_unique(db: AsyncSession) -> None:
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "This deduction or statement line is already matched", code="tds_match_conflict"
        ) from exc


async def confirm(
    ctx: RequestContext, db: AsyncSession, match_id: uuid.UUID, note: str | None = None
) -> TdsMatchOut:
    require_human(actor_from_ctx(ctx))
    m, _line_row, run = await _match(db, match_id)
    _assert_live(run)
    if not m.active or m.status != "proposed":
        raise ConflictError(
            "Only a proposed match can be confirmed", code="invalid_state", details={"status": m.status}
        )
    _decide(ctx, m, "confirmed", note)
    await _flush_unique(db)
    audit.record(
        "tds_recon_match.confirmed", "tds_recon_match", m.id, session=db, run_id=str(run.id), score=m.score
    )
    return await match_out(db, m)


async def reject(
    ctx: RequestContext, db: AsyncSession, match_id: uuid.UUID, note: str | None = None
) -> TdsMatchOut:
    require_human(actor_from_ctx(ctx))
    m, _line_row, run = await _match(db, match_id)
    _assert_live(run)
    if not m.active or m.status not in ("proposed", "confirmed", "unmatched_ack"):
        raise ConflictError(
            "This match cannot be rejected", code="invalid_state", details={"status": m.status}
        )
    previous = m.status
    _decide(ctx, m, "rejected", note)
    await db.flush()
    audit.record(
        "tds_recon_match.rejected", "tds_recon_match", m.id, session=db, run_id=str(run.id), previous=previous
    )
    return await match_out(db, m)


async def _close_open(ctx: RequestContext, db: AsyncSession, line_id: uuid.UUID) -> None:
    for m in (
        await db.execute(
            select(TdsReconMatch).where(
                TdsReconMatch.statement_line_id == line_id,
                TdsReconMatch.active.is_(True),
                TdsReconMatch.status.in_(OPEN),
            )
        )
    ).scalars():
        if m.status != "proposed":
            raise ConflictError("Reject the current decision on this line first", code="tds_line_decided")
        _decide(ctx, m, "rejected", "superseded by a manual decision")
    await db.flush()


async def pair(
    ctx: RequestContext,
    db: AsyncSession,
    line_id: uuid.UUID,
    deduction_id: uuid.UUID,
    *,
    record_tan_mapping: bool = False,
) -> TdsMatchOut:
    require_human(actor_from_ctx(ctx))
    line, run = await _line(db, line_id)
    _assert_live(run)
    ded = await db.get(TdsDeduction, deduction_id)
    start, end = fy_bounds(run.fy)
    if (
        ded is None
        or ded.legal_entity_id != run.legal_entity_id
        or ded.kind != "income_tax"
        or not (start <= ded.deduction_date <= end)
    ):
        raise ValidationFailed(
            "Pick a TDS deduction of this Legal Entity and financial year", code="tds_deduction_invalid"
        )
    if deduction_id in set((await db.execute(reversed_ids_subquery())).scalars()):
        raise ValidationFailed("This TDS deduction was reversed", code="tds_deduction_reversed")
    await _close_open(ctx, db, line.id)
    tans = await tan_map(db)
    s = matching.score(
        matching.LineIn(
            line.id,
            tans.get(line.deductor_tan),
            line.section,
            line.transaction_date,
            Decimal(line.tds_deducted),
        ),
        matching.DeductionIn(
            ded.id, ded.client_id, ded.section_code, Decimal(ded.inr_amount), ded.deduction_date
        ),
    )
    m = TdsReconMatch(
        id=uuid.uuid4(),
        tenant_id=run.tenant_id,
        statement_line_id=line.id,
        tds_deduction_id=ded.id,
        status="proposed",
        score=s.value,
        reasons=[*s.reasons, "manual_pair"],
    )
    db.add(m)
    _decide(ctx, m, "confirmed", None)
    await _flush_unique(db)
    mapped = False
    if record_tan_mapping and line.deductor_tan not in tans:
        db.add(
            TdsDeductorTan(
                id=uuid.uuid4(),
                tenant_id=run.tenant_id,
                client_id=ded.client_id,
                tan=line.deductor_tan,
                created_by=_user(ctx),
            )
        )
        await db.flush()
        mapped = True
    audit.record(
        "tds_recon_match.confirmed",
        "tds_recon_match",
        m.id,
        session=db,
        run_id=str(run.id),
        manual=True,
        tan_mapping_recorded=mapped,
    )
    return await match_out(db, m)


async def acknowledge(ctx: RequestContext, db: AsyncSession, line_id: uuid.UUID, note: str) -> TdsMatchOut:
    require_human(actor_from_ctx(ctx))
    line, run = await _line(db, line_id)
    _assert_live(run)
    await _close_open(ctx, db, line.id)
    m = TdsReconMatch(
        id=uuid.uuid4(),
        tenant_id=run.tenant_id,
        statement_line_id=line.id,
        tds_deduction_id=None,
        status="proposed",
        score=0,
        reasons=[],
    )
    db.add(m)
    _decide(ctx, m, "unmatched_ack", note)
    await _flush_unique(db)
    audit.record("tds_recon_match.acknowledged", "tds_recon_match", m.id, session=db, run_id=str(run.id))
    return await match_out(db, m)


# ---------------------------------------------------------------- TAN mappings
async def list_tans(ctx: RequestContext, db: AsyncSession) -> list[TdsDeductorTanOut]:  # ruff: ignore[unused-function-argument]
    rows = list((await db.execute(select(TdsDeductorTan).order_by(TdsDeductorTan.tan))).scalars())
    names = await client_names(db, {r.client_id for r in rows})
    return [
        TdsDeductorTanOut(
            id=r.id,
            client_id=r.client_id,
            client_name=names.get(r.client_id),
            tan=r.tan,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def create_tan(ctx: RequestContext, db: AsyncSession, data: TdsDeductorTanIn) -> TdsDeductorTanOut:
    from app.models.clients import Client

    if await db.get(Client, data.client_id) is None:
        raise ValidationFailed("Unknown client", code="client_not_found")
    if (
        await db.execute(select(TdsDeductorTan.id).where(TdsDeductorTan.tan == data.tan))
    ).first() is not None:
        raise ConflictError("This TAN is already mapped", code="tan_already_mapped")
    row = TdsDeductorTan(
        id=uuid.uuid4(), tenant_id=_tenant(db), client_id=data.client_id, tan=data.tan, created_by=_user(ctx)
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    audit.record(
        "tds_deductor_tan.created", "tds_deductor_tan", row.id, session=db, client_id=str(row.client_id)
    )
    # Lines of open runs with this TAN become proposable now.
    run_ids = select(TdsStatementLine.run_id).where(TdsStatementLine.deductor_tan == row.tan)
    for run in (
        await db.execute(
            select(TdsReconRun).where(TdsReconRun.id.in_(run_ids), TdsReconRun.status == "ready")
        )
    ).scalars():
        await propose(db, run)
    names = await client_names(db, {row.client_id})
    return TdsDeductorTanOut(
        id=row.id,
        client_id=row.client_id,
        client_name=names.get(row.client_id),
        tan=row.tan,
        created_at=row.created_at,
    )


async def delete_tan(ctx: RequestContext, db: AsyncSession, tan_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    row = await db.get(TdsDeductorTan, tan_id)
    if row is None:
        raise NotFound("TAN mapping not found")
    await db.delete(row)
    await db.flush()
    audit.record("tds_deductor_tan.deleted", "tds_deductor_tan", tan_id, session=db)


__all__ = ["fy_short"]
