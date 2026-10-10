# ruff: noqa: E501
"""Direct-insert seed for the Epic 34 tests: legal entities, clients, issued documents, payments, placements.

Documents are inserted as ``draft`` with their lines and then moved to their final status in one UPDATE, the way
the billing domain does (content-immutability guards allow lines only while the header is mutable).
"""

import itertools
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_seq = itertools.count(1)


def _n() -> int:
    return next(_seq)


async def _insert(db: AsyncSession, table: str, values: dict[str, Any]) -> uuid.UUID:
    values = {"id": uuid.uuid4(), **values}
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    await db.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), values)
    return values["id"]


async def legal_entity(db: AsyncSession, tenant_id: uuid.UUID, name: str = "Initech India") -> uuid.UUID:
    from app.services import legal_entities_service

    le = await _insert(
        db, "legal_entities",
        {"tenant_id": tenant_id, "name": f"{name} {_n()}", "legal_name": f"{name} Pvt Ltd", "default_currency": "INR"},
    )  # fmt: skip
    await legal_entities_service.ensure_periods(db, le, date(2026, 1, 1), date(2027, 3, 1))  # open periods
    return le


async def client(
    db: AsyncSession, tenant_id: uuid.UUID, owner_id: uuid.UUID, name: str = "Acme"
) -> uuid.UUID:
    return await _insert(
        db, "clients", {"tenant_id": tenant_id, "name": f"{name} {_n()}", "owner_id": owner_id}
    )


def _line(tenant_id: uuid.UUID, parent_col: str, parent_id: uuid.UUID, no: int, taxable: Decimal, cost: Decimal,
          placement_id: uuid.UUID | None = None, **extra: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "tenant_id": tenant_id, parent_col: parent_id, "line_no": no, "line_kind": "manual", "description": "Services",
        "sac_code": "998513", "quantity": Decimal("1"), "rate": taxable, "effective_rate": taxable,
        "gross_amount": taxable, "taxable_value": taxable, "inr_taxable_value": taxable, "cost_amount_inr": cost,
        "placement_id": placement_id, **extra,
    }  # fmt: skip


async def invoice(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    le: uuid.UUID,
    client_id: uuid.UUID,
    doc_date: date,
    lines: list[tuple[Decimal, Decimal]],
    *,
    status: str = "issued",
    cancel_basis: str | None = None,
    imported: bool = False,
    tax_pct: Decimal = Decimal("18"),
    number: str | None = None,
    placement_id: uuid.UUID | None = None,
    einvoice_status: str = "not_applicable",
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    taxable = sum((t for t, _c in lines), Decimal(0))
    total = (taxable * (1 + tax_pct / 100)).quantize(Decimal("0.01"))
    inv = await _insert(
        db,
        "invoices",
        {"tenant_id": tenant_id, "legal_entity_id": le, "client_id": client_id, "currency": "INR"},
    )
    line_ids = [
        await _insert(db, "invoice_lines", _line(tenant_id, "invoice_id", inv, i + 1, t, c, placement_id))
        for i, (t, c) in enumerate(lines)
    ]
    await db.execute(
        text(
            "UPDATE invoices SET status=:s, number=:num, doc_date=:d, due_date=:d, issued_at=:at, is_imported=:imp, "
            "cancel_basis=:cb, taxable_total=:tx, inr_taxable_total=:tx, document_total=:tot, inr_document_total=:tot, "
            "einvoice_status=:ei, fiscal_year='26-27' WHERE id=:id"
        ),
        {"s": status, "num": number or f"INV/26-27/{_n():04d}", "d": doc_date, "at": datetime.now(UTC), "imp": imported,
         "cb": cancel_basis, "tx": taxable, "tot": total, "ei": einvoice_status, "id": inv},
    )  # fmt: skip
    return inv, line_ids


async def credit_note(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    le: uuid.UUID,
    client_id: uuid.UUID,
    invoice_id: uuid.UUID,
    invoice_line_id: uuid.UUID,
    doc_date: date,
    taxable: Decimal,
    cost: Decimal = Decimal("0"),
) -> uuid.UUID:
    cn = await _insert(
        db, "credit_notes",
        {"tenant_id": tenant_id, "legal_entity_id": le, "client_id": client_id, "currency": "INR",
         "original_invoice_id": invoice_id, "reason": "commercial"},
    )  # fmt: skip
    await _insert(
        db,
        "credit_note_lines",
        _line(tenant_id, "credit_note_id", cn, 1, taxable, cost, original_invoice_line_id=invoice_line_id),
    )
    total = (taxable * Decimal("1.18")).quantize(Decimal("0.01"))
    await db.execute(
        text(
            "UPDATE credit_notes SET status='issued', number=:num, doc_date=:d, issued_at=:at, taxable_total=:tx, "
            "inr_taxable_total=:tx, document_total=:tot, inr_document_total=:tot, fiscal_year='26-27' WHERE id=:id"
        ),
        {
            "num": f"CN/26-27/{_n():04d}",
            "d": doc_date,
            "at": datetime.now(UTC),
            "tx": taxable,
            "tot": total,
            "id": cn,
        },
    )
    return cn


async def payment(
    db: AsyncSession, tenant_id: uuid.UUID, le: uuid.UUID, client_id: uuid.UUID, day: date, amount: Decimal
) -> uuid.UUID:
    return await _insert(
        db, "payments",
        {"tenant_id": tenant_id, "human_id": f"PAY-{_n():06d}", "legal_entity_id": le, "client_id": client_id,
         "payment_date": day, "amount": amount, "currency": "INR", "inr_amount": amount, "mode": "neft"},
    )  # fmt: skip


async def candidate(
    db: AsyncSession, tenant_id: uuid.UUID, owner_id: uuid.UUID, name: str = "Wendy Worker"
) -> uuid.UUID:
    return await _insert(
        db, "candidates",
        {"tenant_id": tenant_id, "human_id": f"CAN-{_n():06d}", "full_name": name, "owner_id": owner_id, "source": "manual"},
    )  # fmt: skip


async def placement(
    db: AsyncSession, tenant_id: uuid.UUID, le: uuid.UUID, client_id: uuid.UUID, candidate_id: uuid.UUID, *,
    status: str = "active", start: date | None = None, end: date | None = None, am: uuid.UUID | None = None,
    human_id: str | None = None,
) -> uuid.UUID:  # fmt: skip
    return await _insert(
        db, "placements",
        {"tenant_id": tenant_id, "human_id": human_id or f"PLC-{_n():06d}", "candidate_id": candidate_id,
         "client_id": client_id, "legal_entity_id": le, "payee_kind": "worker", "status": status,
         "planned_start_date": start or date(2026, 9, 1), "actual_start_date": start, "end_date": end,
         "currency": "INR", "account_manager_user_id": am},
    )  # fmt: skip
