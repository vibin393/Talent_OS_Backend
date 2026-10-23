"""Payroll file column-mapping templates (Epic 28 A6).

The column catalogue is fixed: row fields, the seven statutory placeholders (always empty), and the version
header fields. A mapping must reference only catalogue fields (422 ``unknown_source_field``) and include at
least one identity column (422 ``identity_column_required``). A tenant default mapping is created lazily.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.payroll import PayrollExportMapping
from app.schemas.payroll import CatalogueField, MappingColumn, MappingIn, MappingOut, MappingUpdate
from app.services.payroll.earnings import PLACEHOLDERS
from app.services.payroll.pay_periods import tenant_of

# (key, label, identity, placeholder)
_CATALOGUE: tuple[tuple[str, str, bool, bool], ...] = (
    ("worker_human_id", "Worker ID", True, False),
    ("worker_name", "Worker name", True, False),
    ("pan", "PAN", True, False),
    ("bank_account", "Bank account", True, False),
    ("ifsc", "IFSC", False, False),
    ("worker_type", "Worker type", False, False),
    ("currency", "Currency", False, False),
    ("period_start", "Period start", False, False),
    ("period_end", "Period end", False, False),
    ("version_kind", "Version", False, False),
    ("version_seq", "Version no.", False, False),
    ("hours_regular", "Regular hours", False, False),
    ("hours_ot", "OT hours", False, False),
    ("hours_double_time", "Double-time hours", False, False),
    ("hours_holiday", "Holiday hours", False, False),
    ("hours_leave_paid", "Paid leave hours", False, False),
    ("hours_leave_unpaid", "Unpaid leave hours", False, False),
    ("days_leave_paid", "Paid leave days", False, False),
    ("days_leave_unpaid", "Unpaid leave days", False, False),
    ("earnings_regular", "Regular earnings", False, False),
    ("earnings_ot", "OT earnings", False, False),
    ("earnings_double_time", "Double-time earnings", False, False),
    ("earnings_holiday", "Holiday earnings", False, False),
    ("earnings_leave_paid", "Paid leave earnings", False, False),
    ("earnings_base", "Monthly base", False, False),
    ("reimbursements_total", "Reimbursements", False, False),
    ("adjustments_total", "Adjustments", False, False),
    ("rate_adjustments_total", "Rate adjustments", False, False),
    ("gross_earnings_inputs", "Gross earnings inputs", False, False),
    ("pf_employee", "PF (employee)", False, True),
    ("pf_employer", "PF (employer)", False, True),
    ("esi_employee", "ESI (employee)", False, True),
    ("esi_employer", "ESI (employer)", False, True),
    ("professional_tax", "Professional tax", False, True),
    ("tds_salary", "TDS (salary)", False, True),
    ("other_deductions", "Other deductions", False, True),
)
CATALOGUE: dict[str, CatalogueField] = {
    k: CatalogueField(key=k, label=lbl, identity=i, placeholder=p) for k, lbl, i, p in _CATALOGUE
}
IDENTITY_FIELDS = frozenset(k for k, f in CATALOGUE.items() if f.identity)
assert set(PLACEHOLDERS) <= set(CATALOGUE)


def default_columns() -> list[MappingColumn]:
    return [MappingColumn(source_field=k, header=f.label, include=True) for k, f in CATALOGUE.items()]


def catalogue() -> list[CatalogueField]:
    return list(CATALOGUE.values())


def validate_columns(columns: list[MappingColumn]) -> None:
    unknown = sorted({c.source_field for c in columns} - set(CATALOGUE))
    if unknown:
        raise ValidationFailed(
            "Unknown source field", code="unknown_source_field", details={"fields": unknown}
        )
    if not any(c.include and c.source_field in IDENTITY_FIELDS for c in columns):
        raise ValidationFailed("Include at least one identity column", code="identity_column_required")


def _out(m: PayrollExportMapping) -> MappingOut:
    return MappingOut(
        id=m.id,
        legal_entity_id=m.legal_entity_id,
        name=m.name,
        format=m.format,
        columns=[MappingColumn(**c) for c in m.columns or []],
        is_default=m.is_default,
    )


async def _clear_default(db: AsyncSession, legal_entity_id: uuid.UUID | None, keep: uuid.UUID | None) -> None:
    stmt = update(PayrollExportMapping).where(PayrollExportMapping.is_default.is_(True))
    stmt = stmt.where(
        PayrollExportMapping.legal_entity_id.is_(None)
        if legal_entity_id is None
        else PayrollExportMapping.legal_entity_id == legal_entity_id
    )
    if keep is not None:
        stmt = stmt.where(PayrollExportMapping.id != keep)
    await db.execute(stmt.values(is_default=False))


async def ensure_default(db: AsyncSession) -> PayrollExportMapping:
    m = (
        await db.execute(
            select(PayrollExportMapping).where(
                PayrollExportMapping.legal_entity_id.is_(None), PayrollExportMapping.is_default.is_(True)
            )
        )
    ).scalar_one_or_none()
    if m is None:
        m = PayrollExportMapping(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            legal_entity_id=None,
            name="Default",
            format="csv",
            columns=[c.model_dump() for c in default_columns()],
            is_default=True,
        )
        db.add(m)
        await db.flush()
    return m


async def resolve(
    db: AsyncSession, mapping_id: uuid.UUID | None, legal_entity_id: uuid.UUID
) -> PayrollExportMapping:
    if mapping_id is not None:
        m = await db.get(PayrollExportMapping, mapping_id)
        if m is None or str(m.tenant_id) != str(tenant_of(db)):
            raise NotFound("Mapping not found")
        return m
    le_default = (
        await db.execute(
            select(PayrollExportMapping).where(
                PayrollExportMapping.legal_entity_id == legal_entity_id,
                PayrollExportMapping.is_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    return le_default or await ensure_default(db)


async def list_mappings(ctx: RequestContext, db: AsyncSession) -> list[MappingOut]:  # ruff: ignore[unused-function-argument]
    await ensure_default(db)
    rows = await db.execute(
        select(PayrollExportMapping).order_by(PayrollExportMapping.name, PayrollExportMapping.id)
    )
    return [_out(m) for m in rows.scalars()]


async def create_mapping(ctx: RequestContext, db: AsyncSession, data: MappingIn) -> MappingOut:  # ruff: ignore[unused-function-argument]
    validate_columns(data.columns)
    m = PayrollExportMapping(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        legal_entity_id=data.legal_entity_id,
        name=data.name,
        format=data.format,
        columns=[c.model_dump() for c in data.columns],
        is_default=False,
    )
    db.add(m)
    await db.flush()
    if data.is_default:
        await _clear_default(db, m.legal_entity_id, m.id)
        m.is_default = True
        await db.flush()
    audit.record("payroll.mapping_created", "payroll_export_mapping", m.id, session=db)
    return _out(m)


async def update_mapping(
    ctx: RequestContext, db: AsyncSession, mapping_id: uuid.UUID, data: MappingUpdate
) -> MappingOut:
    del ctx
    m = await resolve(db, mapping_id, uuid.uuid4())
    if data.columns is not None:
        validate_columns(data.columns)
        m.columns = [c.model_dump() for c in data.columns]
    if data.name is not None:
        m.name = data.name
    if data.format is not None:
        m.format = data.format
    if data.is_default:
        await _clear_default(db, m.legal_entity_id, m.id)
        m.is_default = True
    elif data.is_default is False:
        m.is_default = False
    await db.flush()
    audit.record("payroll.mapping_updated", "payroll_export_mapping", m.id, session=db)
    return _out(m)
