"""Retention bindings consumer (Epic 35 A3, AD-34, FR-180).

Domains bind their tables to a retention class with ``register_retention_binding`` (foundation R15, registry in
``retention_classes``). This module *consumes* the registry:

* ``rows_for_candidate`` -- the bound rows of one data subject, found generically through table metadata
  (``candidate_id`` column, ``placement_id`` via ``placements``, ``owner_kind/owner_id``);
* ``anchor_of`` -- the row's anchor date, via the binding's ``anchor_resolver``. Resolvers registered before
  this epic disagree on the argument (row id vs candidate id), so the row id is tried first and the candidate id
  second; ``None`` means the anchor has not happened yet (e.g. the placement has not ended) and the row is kept;
* ``retained_until`` -- anchor (+ FY end for ``fy_end``) + the class period, extended by a ``retention_overrides``
  row;
* ``retention_for_erasure`` -- ``(ErasureContext.retained, retained_summary)`` for a DSR / retention erasure: a
  legal hold keeps everything bound; otherwise a row is kept while ``retained_until`` is in the future.

Fallback bindings for statutory tables whose owners did not register one (``invoice_lines``, ``timesheets``,
``timesheet_entries``, ``expense_claims``, ``expense_lines``) are registered here
(``financial``), lazily on first use so owner registrations win; their ``expire_fn`` is report-only (financial rows are DB-immutable,
AD-21 -- expiry deferred until the spine amendment is ratified).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from dateutil.relativedelta import relativedelta
from loguru import logger
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Base
from app.models.privacy import LegalHold, RetentionClass, RetentionOverride
from app.services.privacy.retention_classes import (
    RetentionBinding,
    register_retention_binding,
    retention_bindings,
)

# Tables the governance coverage expects to be bound once their owner lands (table -> (class, owner epic)).
EXPECTED_BINDINGS: dict[str, tuple[str, str]] = {
    "invoices": ("financial", "Epic 29"),
    "invoice_lines": ("financial", "Epic 29"),
    "credit_notes": ("financial", "Epic 29"),
    "debit_notes": ("financial", "Epic 29"),
    "payments": ("financial", "Epic 30"),
    "write_offs": ("financial", "Epic 30"),
    "timesheets": ("financial", "Epic 25"),
    "timesheet_entries": ("financial", "Epic 25"),
    "expense_claims": ("financial", "Epic 27"),
    "expense_lines": ("financial", "Epic 27"),
    "payroll_exports": ("financial", "Epic 28"),
    "vendor_statements": ("financial", "Epic 17"),
    "documents": ("signed_agreements", "Epic 22"),
    "signatures": ("signed_agreements", "Epic 22"),
    "bgv_evidence": ("bgv_evidence", "Epic 20"),
    "worker_profiles": ("worker_profile", "Epic 19"),
    "worker_bank_accounts": ("worker_profile", "Epic 19"),
    "credentials": ("worker_profile", "Epic 21"),
    "onboarding_tasks": ("worker_profile", "Epic 23"),
}

FINANCIAL_REASON = "immutable record, awaiting policy"


@dataclass(frozen=True)
class BoundRow:
    table: str
    row_id: uuid.UUID
    candidate_id: uuid.UUID | None


def fy_end(d: date) -> date:
    """End of the Indian financial year (31 March) containing ``d``."""
    return date(d.year if d.month <= 3 else d.year + 1, 3, 31)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return None


def until_for(anchor: date | None, cls: RetentionClass) -> date | None:
    """``retained_until`` of a row with ``anchor`` under ``cls`` (``None`` = anchor not reached yet)."""
    if anchor is None:
        return None
    base = fy_end(anchor) if cls.anchor == "fy_end" else anchor
    return base + relativedelta(months=cls.min_retention_months)  # type: ignore[no-any-return]


async def anchor_of(db: AsyncSession, binding: RetentionBinding, row: BoundRow) -> date | None:
    for arg in (row.row_id, row.candidate_id):
        if arg is None:
            continue
        try:
            value = await binding.anchor_resolver(db, arg)
        except TypeError:
            continue
        except Exception:  # a resolver must never break erasure
            logger.exception("retention anchor resolver for {} failed", binding.table)
            return None
        found = _as_date(value)
        if found is not None:
            return found
    return None


async def _subject_ids(db: AsyncSession, candidate_id: uuid.UUID) -> list[uuid.UUID]:
    candidates = Base.metadata.tables["candidates"]
    merged = (
        await db.execute(select(candidates.c.id).where(candidates.c.merged_into_id == candidate_id))
    ).scalars()
    return [candidate_id, *merged]


async def rows_for_candidate(db: AsyncSession, table_name: str, candidate_id: uuid.UUID) -> list[BoundRow]:
    table: Table | None = Base.metadata.tables.get(table_name)
    if table is None or "id" not in table.c:
        return []
    ids = await _subject_ids(db, candidate_id)
    cols = table.c
    if "candidate_id" in cols:
        stmt = select(cols.id).where(cols.candidate_id.in_(ids))
    elif "placement_id" in cols and "placements" in Base.metadata.tables:
        placements = Base.metadata.tables["placements"]
        stmt = select(cols.id).where(
            cols.placement_id.in_(select(placements.c.id).where(placements.c.candidate_id.in_(ids)))
        )
    elif "owner_kind" in cols and "owner_id" in cols:
        stmt = select(cols.id).where(cols.owner_kind.in_(("candidate", "worker")), cols.owner_id.in_(ids))
    else:
        return []  # resolved by the owner's erasure handler (it honours ctx.retained itself)
    return [BoundRow(table_name, rid, candidate_id) for rid in (await db.execute(stmt)).scalars()]


async def classes_by_key(db: AsyncSession) -> dict[str, RetentionClass]:
    from app.services.privacy.retention_classes import seed_system_classes

    await seed_system_classes(db)
    rows = (await db.execute(select(RetentionClass).where(RetentionClass.archived_at.is_(None)))).scalars()
    return {r.key: r for r in rows}


async def overrides_for(
    db: AsyncSession, subject_kind: str, subject_ids: Sequence[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], date]:
    rows = (
        await db.execute(
            select(RetentionOverride).where(
                RetentionOverride.subject_kind == subject_kind,
                RetentionOverride.subject_id.in_(list(subject_ids)),
            )
        )
    ).scalars()
    return {(r.subject_id, r.class_key): r.until_date for r in rows}


async def retention_for_erasure(
    db: AsyncSession, candidate_id: uuid.UUID, *, today: date | None = None
) -> tuple[dict[str, set[uuid.UUID]], list[dict[str, Any]]]:
    """``(retained ids per table, retained_summary)`` for erasing ``candidate_id`` now."""
    today = today or datetime.now(UTC).date()
    register_fallback_bindings()
    classes = await classes_by_key(db)
    held = (
        await db.execute(
            select(LegalHold.id)
            .where(LegalHold.candidate_id == candidate_id, LegalHold.released_at.is_(None))
            .limit(1)
        )
    ).first() is not None
    overrides = await overrides_for(db, "candidate", [candidate_id])
    retained: dict[str, set[uuid.UUID]] = {}
    summary: dict[tuple[str, str, str], dict[str, Any]] = {}
    for table_name, binding in sorted(retention_bindings().items()):
        cls = classes.get(binding.class_key)
        if cls is None:
            continue
        for row in await rows_for_candidate(db, table_name, candidate_id):
            if held:
                until, reason = None, "legal_hold"
            else:
                anchor = await anchor_of(db, binding, row)
                until = until_for(anchor, cls)
                reason = "statutory_retention" if anchor is not None else "anchor_pending"
                override = overrides.get((candidate_id, cls.key))
                if override is not None and (until is None or override > until) and anchor is not None:
                    until, reason = override, "retention_override"
                if until is not None and until <= today:
                    continue  # retention period over: erasable
            retained.setdefault(table_name, set()).add(row.row_id)
            key = (cls.key, table_name, reason)
            entry = summary.setdefault(
                key,
                {"class": cls.key, "table": table_name, "count": 0, "retained_until": None, "reason": reason},
            )
            entry["count"] += 1
            if until is not None and (
                entry["retained_until"] is None or until.isoformat() > entry["retained_until"]
            ):
                entry["retained_until"] = until.isoformat()
    return retained, list(summary.values())


# ================================================================ fallback bindings (no owner registration)
async def _report_only_expire(*_: Any, **__: Any) -> dict[str, int]:  # NOSONAR
    """Financial rows are DB-immutable (AD-21): expiry is report-only until the spine amendment."""
    return {}


def _row_subject(table_name: str) -> Any:
    async def subject(db: AsyncSession, row_id: uuid.UUID) -> uuid.UUID | None:
        table = Base.metadata.tables[table_name]
        if "candidate_id" in table.c:
            return (
                await db.execute(select(table.c.candidate_id).where(table.c.id == row_id))
            ).scalar_one_or_none()
        placements = Base.metadata.tables["placements"]
        return (
            await db.execute(
                select(placements.c.candidate_id)
                .join(table, table.c.placement_id == placements.c.id)
                .where(table.c.id == row_id)
            )
        ).scalar_one_or_none()

    return subject


_DATE_COLUMNS = (
    "period_end",
    "issued_at",
    "document_date",
    "expense_date",
    "work_date",
    "submitted_at",
    "created_at",
)


def _row_anchor(table_name: str) -> Any:
    async def anchor(db: AsyncSession, row_id: uuid.UUID) -> Any:
        table = Base.metadata.tables[table_name]
        # invoice lines take the issue date of their invoice
        if "invoice_id" in table.c and "invoices" in Base.metadata.tables:
            invoices = Base.metadata.tables["invoices"]
            issued = (
                await db.execute(
                    select(invoices.c.issued_at)
                    .join(table, table.c.invoice_id == invoices.c.id)
                    .where(table.c.id == row_id)
                )
            ).scalar_one_or_none()
            if issued is not None:
                return issued
        for col in _DATE_COLUMNS:
            if col in table.c:
                return (
                    await db.execute(select(table.c[col]).where(table.c.id == row_id))
                ).scalar_one_or_none()
        return None

    return anchor


FALLBACK_BINDINGS: Mapping[str, str] = {
    "invoice_lines": "financial",
    "timesheets": "financial",
    "timesheet_entries": "financial",
    "expense_claims": "financial",
    "expense_lines": "financial",
}


def register_fallback_bindings() -> None:
    existing = retention_bindings()
    for table_name, class_key in FALLBACK_BINDINGS.items():
        if table_name in existing or table_name not in Base.metadata.tables:
            continue
        try:
            register_retention_binding(
                table_name,
                class_key,
                subject_resolver=_row_subject(table_name),
                anchor_resolver=_row_anchor(table_name),
                expire_fn=_report_only_expire,
            )
        except Exception:  # pragma: no cover - concurrent registration
            logger.debug("retention binding {} already registered", table_name)


def bindings_overview() -> list[dict[str, Any]]:
    register_fallback_bindings()
    registered = retention_bindings()
    out = [
        {
            "table": t,
            "class_key": b.class_key,
            "status": "registered",
            "owner_epic": EXPECTED_BINDINGS.get(t, ("", None))[1],
        }
        for t, b in sorted(registered.items())
    ]
    out += [
        {"table": t, "class_key": cls, "status": "pending", "owner_epic": epic}
        for t, (cls, epic) in sorted(EXPECTED_BINDINGS.items())
        if t not in registered
    ]
    return out
