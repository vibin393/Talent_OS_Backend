"""Posting ledgers -- the single writer (AD-23, NFR-25): approved work is billed and paid exactly once, by
quantity.

Billing, payroll and vendor statements **pull** unposted source items at run time (``load_unposted`` over the
registered source loaders); source domains never push lines. ``reserve`` (billing draft build) and
``record_posted`` (payroll finalize / statements) insert inside a savepoint: the partial UNIQUE
``(tenant_id, source_kind, source_id) WHERE state <> 'released'`` turns a second active posting of the same
source into 409 ``source_already_posted`` while the outer transaction stays usable. ``post`` flips reserved rows
to ``posted`` (issue); ``release`` frees them (discard, full credit note, IRN cancellation) so the source can be
posted again. Rows are immutable at the database except the state columns.

AD-32: this module sits above ``time`` / ``expenses`` and below ``billing`` / ``payroll``; it imports neither.

Standard ``SourceItem.attributes`` keys (loaders set those that apply; consumers read only these): ``category``,
``billable``, ``payable``, ``candidate_id``, ``legal_entity_id``, ``vendor_id``, ``approved_at``, ``unit``
(``hour|day|item|month``), ``rate_delta``, ``original_source_kind``, ``original_source_id``, ``rebill_method``,
``markup_pct``, ``rebill_tax_mode``, ``reimbursable``.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.money import DecimalStr
from app.core.registry import Registry
from app.models.postings import (
    BILLING_TARGET_KINDS,
    PAYABLE_TARGET_KINDS,
    PAYEE_KINDS,
    BillingPosting,
    PayablePosting,
)

Ledger = Literal["billing", "payable"]
LEDGERS: tuple[Ledger, ...] = ("billing", "payable")
ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "category",
        "billable",
        "payable",
        "candidate_id",
        "legal_entity_id",
        "vendor_id",
        "approved_at",
        "unit",
        "rate_delta",
        "original_source_kind",
        "original_source_id",
        "rebill_method",
        "markup_pct",
        "rebill_tax_mode",
        "reimbursable",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class SourceQuery:
    ledger: Ledger
    period_end: date
    period_start: date | None = None
    placement_ids: Sequence[uuid.UUID] | None = None
    legal_entity_id: uuid.UUID | None = None
    payee_kind: Literal["worker", "vendor"] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class SourceItem:
    source_kind: str
    source_id: uuid.UUID
    placement_id: uuid.UUID | None
    item_date: date
    quantity: Decimal
    amount: Decimal | None  # ledger-specific (bill vs pay amount)
    currency: str
    attributes: Mapping[str, Any]


SourceLoader = Callable[[RequestContext, AsyncSession, SourceQuery], Awaitable[list[SourceItem]]]


@dataclasses.dataclass(frozen=True, slots=True)
class PostingLine:
    source_kind: str
    source_id: uuid.UUID
    target_kind: str
    target_id: uuid.UUID
    document_id: uuid.UUID | None
    quantity: Decimal
    amount: Decimal
    currency: str
    placement_id: uuid.UUID | None = None
    payee_kind: str | None = None


class PostingOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    ledger: Ledger
    source_kind: str
    source_id: uuid.UUID
    target_kind: str
    target_id: uuid.UUID
    document_id: uuid.UUID | None
    placement_id: uuid.UUID | None
    payee_kind: str | None
    quantity: DecimalStr
    amount: DecimalStr
    currency: str
    state: str
    reserved_at: datetime
    posted_at: datetime | None


@dataclasses.dataclass(frozen=True, slots=True)
class _SourceDef:
    loader: SourceLoader | None
    ledgers: frozenset[Ledger]


_sources: Registry[str, _SourceDef] = Registry("posting source")


def register_posting_source(kind: str, loader: SourceLoader | None, *, ledgers: frozenset[Ledger]) -> None:
    if not kind or len(kind) > 32:
        msg = f"invalid posting source kind {kind!r}"
        raise ValueError(msg)
    unknown = set(ledgers) - set(LEDGERS)
    if unknown or not ledgers:
        msg = f"invalid ledgers {sorted(ledgers)!r}"
        raise ValueError(msg)
    _sources.register(kind, _SourceDef(loader, frozenset(ledgers)))


# "manual" postings have no loader (hand-entered lines) and may go to either ledger.
register_posting_source("manual", None, ledgers=frozenset({"billing", "payable"}))


def posting_sources() -> Mapping[str, _SourceDef]:
    return _sources.view()


def _model(ledger: Ledger) -> type[BillingPosting] | type[PayablePosting]:
    if ledger == "billing":
        return BillingPosting
    if ledger == "payable":
        return PayablePosting
    msg = f"unknown ledger {ledger!r}"  # type: ignore[unreachable]
    raise ValueError(msg)


def _tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "postings need a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def _now() -> datetime:
    return datetime.now(UTC)


def _out(row: BillingPosting | PayablePosting, ledger: Ledger) -> PostingOut:
    return PostingOut(
        id=row.id,
        ledger=ledger,
        source_kind=row.source_kind,
        source_id=row.source_id,
        target_kind=row.target_kind,
        target_id=row.target_id,
        document_id=row.document_id,
        placement_id=row.placement_id,
        payee_kind=getattr(row, "payee_kind", None),
        quantity=row.quantity,
        amount=row.amount,
        currency=row.currency,
        state=row.state,
        reserved_at=row.reserved_at,
        posted_at=row.posted_at,
    )


def _check_kind(kind: str, ledger: Ledger) -> None:
    definition = _sources.find(kind)
    if definition is None or ledger not in definition.ledgers:
        raise ValidationFailed(
            "Unknown posting source", code="unknown_posting_source", details={"source_kind": kind, "ledger": ledger}
        )


def _check_line(ledger: Ledger, line: PostingLine) -> None:
    _check_kind(line.source_kind, ledger)
    if not isinstance(line.amount, Decimal) or not isinstance(line.quantity, Decimal):
        msg = "posting amounts and quantities must be Decimal"  # type: ignore[unreachable]
        raise TypeError(msg)
    targets = BILLING_TARGET_KINDS if ledger == "billing" else PAYABLE_TARGET_KINDS
    if line.target_kind not in targets:
        raise ValidationFailed(
            "Invalid posting target", code="invalid_posting_target", details={"target_kind": line.target_kind}
        )
    if ledger == "payable" and line.payee_kind not in PAYEE_KINDS:
        raise ValidationFailed("A payable posting needs a payee kind", code="invalid_payee_kind")


async def _insert(ctx: RequestContext, db: AsyncSession, ledger: Ledger, lines: Sequence[PostingLine], state: str) -> list[uuid.UUID]:
    if not lines:
        return []
    for line in lines:
        _check_line(ledger, line)
    model = _model(ledger)
    actor = actor_from_ctx(ctx)
    tenant_id = _tenant(db)
    now = _now()
    rows = []
    for line in lines:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "source_kind": line.source_kind,
            "source_id": line.source_id,
            "target_kind": line.target_kind,
            "target_id": line.target_id,
            "document_id": line.document_id,
            "placement_id": line.placement_id,
            "quantity": line.quantity,
            "amount": line.amount,
            "currency": line.currency.upper(),
            "state": state,
            "reserved_at": now,
            "posted_at": now if state == "posted" else None,
            "created_by_kind": actor.kind,
            "created_by_id": actor.id,
        }
        if ledger == "payable":
            values["payee_kind"] = line.payee_kind
        rows.append(model(**values))
    try:
        async with db.begin_nested():
            db.add_all(rows)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "A source item is already posted",
            code="source_already_posted",
            details={
                "source_kind": sorted({line.source_kind for line in lines}),
                "source_ids": sorted({str(line.source_id) for line in lines}),
            },
        ) from exc
    return [r.id for r in rows]


async def reserve(ctx: RequestContext, db: AsyncSession, ledger: Ledger, lines: Sequence[PostingLine]) -> list[uuid.UUID]:
    """Billing draft build: insert ``reserved`` postings (409 ``source_already_posted`` when active)."""
    return await _insert(ctx, db, ledger, lines, "reserved")


async def record_posted(
    ctx: RequestContext, db: AsyncSession, ledger: Ledger, lines: Sequence[PostingLine]
) -> list[uuid.UUID]:
    """Payroll finalize / vendor statements: insert directly as ``posted``."""
    return await _insert(ctx, db, ledger, lines, "posted")


def _selector(model: Any, document_id: uuid.UUID | None, posting_ids: Sequence[uuid.UUID] | None) -> list[Any]:
    if document_id is None and not posting_ids:
        msg = "give document_id or posting_ids"
        raise ValueError(msg)
    conds: list[Any] = []
    if document_id is not None:
        conds.append(model.document_id == document_id)
    if posting_ids:
        conds.append(model.id.in_(list(posting_ids)))
    return conds


async def post(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ledger: Ledger,
    *,
    document_id: uuid.UUID | None = None,
    posting_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Flip ``reserved`` postings of a document (or the given ids) to ``posted``; returns the count."""
    model = _model(ledger)
    result = await db.execute(
        update(model)
        .where(*_selector(model, document_id, posting_ids), model.state == "reserved")
        .values(state="posted", posted_at=_now())
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def release(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ledger: Ledger,
    *,
    reason: str,
    document_id: uuid.UUID | None = None,
    posting_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Release the active postings of a document (or the given ids); the sources can be posted again."""
    if not (reason or "").strip():
        raise ValidationFailed("A release reason is required", code="reason_required")
    model = _model(ledger)
    result = await db.execute(
        update(model)
        .where(*_selector(model, document_id, posting_ids), model.state != "released")
        .values(state="released", released_at=_now(), release_reason=reason.strip()[:200])
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def targets_for(db: AsyncSession, ledger: Ledger, source_kind: str, source_id: uuid.UUID) -> list[PostingOut]:
    model = _model(ledger)
    rows = await db.execute(
        select(model)
        .where(model.source_kind == source_kind, model.source_id == source_id)
        .order_by(model.reserved_at, model.id)
    )
    return [_out(r, ledger) for r in rows.scalars()]  # type: ignore[arg-type]


async def sources_for(db: AsyncSession, ledger: Ledger, target_kind: str, target_id: uuid.UUID) -> list[PostingOut]:
    model = _model(ledger)
    rows = await db.execute(
        select(model)
        .where(model.target_kind == target_kind, model.target_id == target_id)
        .order_by(model.reserved_at, model.id)
    )
    return [_out(r, ledger) for r in rows.scalars()]  # type: ignore[arg-type]


async def has_active_posting(db: AsyncSession, source_kind: str, source_id: uuid.UUID) -> bool:
    """True when either ledger holds a non-released posting of the source."""
    for ledger in LEDGERS:
        model = _model(ledger)
        found = (
            await db.execute(
                select(model.id)
                .where(model.source_kind == source_kind, model.source_id == source_id, model.state != "released")
                .limit(1)
            )
        ).first()
        if found is not None:
            return True
    return False


async def active_postings_for(
    db: AsyncSession, source_kind: str, source_ids: Sequence[uuid.UUID], *, ledger: Ledger | None = None
) -> dict[uuid.UUID, list[PostingOut]]:
    """Non-released postings per source id (one query per ledger)."""
    out: dict[uuid.UUID, list[PostingOut]] = {sid: [] for sid in source_ids}
    if not source_ids:
        return out
    for name in (ledger,) if ledger is not None else LEDGERS:
        model = _model(name)
        rows = await db.execute(
            select(model).where(
                model.source_kind == source_kind,
                model.source_id.in_(list(source_ids)),
                model.state != "released",
            )
        )
        for row in rows.scalars():
            out.setdefault(row.source_id, []).append(_out(row, name))  # type: ignore[arg-type]
    return out


async def load_unposted(ctx: RequestContext, db: AsyncSession, query: SourceQuery) -> list[SourceItem]:
    """Items of every source kind registered for ``query.ledger``, minus those with an active posting in it."""
    items: list[SourceItem] = []
    for kind, definition in sorted(_sources.view().items()):
        if definition.loader is None or query.ledger not in definition.ledgers:
            continue
        loaded = await definition.loader(ctx, db, query)
        if not loaded:
            continue
        active = await active_postings_for(db, kind, [i.source_id for i in loaded], ledger=query.ledger)
        items.extend(i for i in loaded if not active.get(i.source_id))
    return items
