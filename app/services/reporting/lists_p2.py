# ruff: noqa: E501, ARG001, N812
"""Phase 2 list kinds (Epic 34, A4, FR-178): drill-down lists and export sources over Phase 2 tables.

Registered into the P1 ``lists`` registry (``register_list_kind``), so ``GET /reports/lists/{kind}`` and the
P1 export engine (limits, formula-injection guard, money two-column format, audit) serve them unchanged.

* Read through ``p2tables`` projections (AD-1 read-model exception); a kind whose table is missing -> 404
  ``not_available``.
* Compensation columns (rates, margin, Opportunity value, Vendor rate) are masked / removed exactly like P1.
* Finance amounts (``invoices``, ``payments``, ``expense_claims``) are exported only with
  ``report:finance_export`` (``Column.finance``).
* Identity / bank data (NFR-24, AD-35): no decrypted value is ever a column -- not even with ``identity:view``.
  BGV lists carry no evidence content and no discrepancy text.
"""

import uuid
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, Select, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal, scope_condition
from app.models.candidates import Candidate
from app.models.clients import Client
from app.schemas.reporting import CsvParam, SortParam, split_csv
from app.services.reporting import lists
from app.services.reporting import p2tables as T
from app.services.reporting.common import users
from app.services.reporting.filters import perm
from app.services.reporting.lists import Column, ListKindDef, register_list_kind


class P2ListFilters(BaseModel):
    """Filters shared by the Phase 2 list kinds (business dates compared as dates, AD-14)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: Any = Field(default=None, alias="from")
    to: Any = None
    legal_entity_ids: list[uuid.UUID] | None = None
    client_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    status: CsvParam | None = None
    q: CsvParam | None = None
    sort: SortParam | None = None
    src: CsvParam | None = None  # "dashboard" marker added by drill-down links (ignored)


def _date_range(f: P2ListFilters) -> tuple[Any, Any]:
    from datetime import date

    def parse(v: Any) -> Any:
        return v if v is None or isinstance(v, date) else date.fromisoformat(str(v))

    start, end = parse(f.from_), parse(f.to)
    if start is not None and end is not None and start > end:
        raise lists._field_error("`from` must not be after `to`", "invalid_range")
    return start, end


def _common(stmt: Select[Any], f: P2ListFilters, *, date_col: Any = None, le_col: Any = None, client_col: Any = None,
            status_col: Any = None, vendor_col: Any = None) -> Select[Any]:  # fmt: skip
    start, end = _date_range(f)
    if date_col is not None:
        if start is not None:
            stmt = stmt.where(date_col >= start)
        if end is not None:
            stmt = stmt.where(date_col <= end)
    if le_col is not None and f.legal_entity_ids:
        stmt = stmt.where(le_col.in_(f.legal_entity_ids))
    if client_col is not None and f.client_id:
        stmt = stmt.where(client_col == f.client_id)
    if vendor_col is not None and f.vendor_id:
        stmt = stmt.where(vendor_col == f.vendor_id)
    statuses = split_csv(f.status)
    if status_col is not None and statuses:
        stmt = stmt.where(status_col.in_(statuses))
    return stmt


def _need(avail: frozenset[str], *tables: str) -> None:
    if not set(tables) <= avail:
        raise NotFound("This list is not available yet", code="not_available")


def _client_name(col: Any) -> Any:
    return select(Client.name).where(Client.id == col).scalar_subquery()


def _candidate_name(col: Any) -> Any:
    return select(Candidate.full_name).where(Candidate.id == col).scalar_subquery()


def _user_name(col: Any) -> Any:
    return select(users.c.name).where(users.c.id == col).scalar_subquery()


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _client_scope(principal: Principal, permission: str, client_col: Any) -> ColumnElement[bool] | None:
    cond = scope_condition(principal, perm(permission), Client.owner_id)
    return None if cond is None else client_col.in_(select(Client.id).where(cond))


# ================================================================ placements
P = T.placements.c
RS = T.rate_segments.c


def _segment(attr: Any) -> Any:
    return (
        select(attr)
        .where(RS.placement_id == P.id, RS.status == "active")
        .order_by(RS.effective_from.desc())
        .limit(1)
        .scalar_subquery()
    )


def placement_scope(principal: Principal) -> ColumnElement[bool] | None:
    a = scope_condition(principal, "placement:read", P.recruiter_user_id)
    if a is None:
        return None
    b = scope_condition(principal, "placement:read", P.account_manager_user_id)
    return or_(a, b) if b is not None else a


async def _placements(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "placements")
    stmt = select(P.id)
    cond = placement_scope(principal)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _common(stmt, f, date_col=P.actual_start_date, le_col=P.legal_entity_id, client_col=P.client_id,
                   status_col=P.status, vendor_col=P.vendor_id)  # fmt: skip
    if f.q:
        term = f.q.strip()
        exact = func.upper(P.human_id) == term.upper()
        stmt = stmt.where(
            or_(
                exact,
                P.candidate_id.in_(
                    select(Candidate.id).where(Candidate.full_name.ilike(f"%{_escape(term)}%"))
                ),
            )
        )
        # FR-178: an exact human id sorts first.
        return lists._order(stmt.order_by(exact.desc()), "placements", f.sort, P.id)
    return lists._order(stmt, "placements", f.sort, P.id)


PLACEMENT_COLUMNS = (
    Column("human_id", "Placement ID", "string", P.human_id),
    Column("worker", "Worker", "string", _candidate_name(P.candidate_id), pii=True),
    Column("client", "Client", "string", _client_name(P.client_id)),
    Column("role_title", "Role", "string", P.role_title),
    Column("status", "Status", "enum", P.status),
    Column("planned_start_date", "Planned start", "date", P.planned_start_date),
    Column("actual_start_date", "Start", "date", P.actual_start_date),
    Column("end_date", "End", "date", P.end_date),
    Column("account_manager", "Account manager", "string", _user_name(P.account_manager_user_id)),
    Column("pay_rate", "Pay rate", "money", _segment(RS.pay_rate), _segment(RS.currency), compensation=True),
    Column(
        "bill_rate", "Bill rate", "money", _segment(RS.bill_rate), _segment(RS.currency), compensation=True
    ),
    Column(
        "vendor_rate",
        "Vendor rate",
        "money",
        _segment(RS.vendor_rate),
        _segment(RS.currency),
        compensation=True,
    ),
    Column("margin", "Margin", "money", _segment(RS.margin_amount), _segment(RS.currency), compensation=True),
    Column("margin_pct", "Margin %", "decimal", _segment(RS.margin_pct), compensation=True),
)


# ================================================================ timesheets
TS = T.timesheets.c


async def _timesheets(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "timesheets", "placements")
    stmt = select(TS.id)
    cond = placement_scope(principal)
    if cond is not None:
        stmt = stmt.where(TS.placement_id.in_(select(P.id).where(cond)))
    stmt = _common(
        stmt,
        f,
        date_col=TS.period_start,
        le_col=TS.legal_entity_id,
        client_col=TS.client_id,
        status_col=TS.status,
    )
    return lists._order(stmt, "timesheets", f.sort, TS.id)


TIMESHEET_COLUMNS = (
    Column("human_id", "Timesheet", "string", TS.human_id),
    Column("worker", "Worker", "string", _candidate_name(TS.candidate_id), pii=True),
    Column("client", "Client", "string", _client_name(TS.client_id)),
    Column("period_start", "Period start", "date", TS.period_start),
    Column("period_end", "Period end", "date", TS.period_end),
    Column("status", "Status", "enum", TS.status),
    Column("kind", "Kind", "enum", TS.kind),
    Column("submitted_at", "Submitted", "datetime", TS.submitted_at),
    Column("approved_at", "Approved", "datetime", TS.approved_at),
)


# ================================================================ expense claims
EC = T.expense_claims.c


async def _expense_claims(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "expense_claims", "placements")
    stmt = select(EC.id)
    cond = placement_scope(principal) if principal.has("placement:read") else None
    if cond is not None:
        stmt = stmt.where(EC.placement_id.in_(select(P.id).where(cond)))
    stmt = _common(stmt, f, le_col=EC.legal_entity_id, client_col=EC.client_id, status_col=EC.status)
    return lists._order(stmt, "expense_claims", f.sort, EC.id)


EXPENSE_COLUMNS = (
    Column("human_id", "Claim", "string", EC.human_id),
    Column("title", "Title", "string", EC.title),
    Column("worker", "Worker", "string", _candidate_name(EC.candidate_id), pii=True),
    Column("client", "Client", "string", _client_name(EC.client_id)),
    Column("status", "Status", "enum", EC.status),
    Column("total_claimed", "Claimed", "money", EC.total_claimed, EC.currency, finance=True),
    Column("total_approved", "Approved", "money", EC.total_approved, EC.currency, finance=True),
    Column("submitted_at", "Submitted", "datetime", EC.submitted_at),
)


# ================================================================ invoices
IV = T.invoices.c


def _payment_status() -> Any:
    from sqlalchemy import case

    return case(
        (IV.status == "cancelled", "cancelled"), (IV.due_date < func.current_date(), "overdue"), else_="open"
    )


async def _invoices(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "invoices")
    stmt = select(IV.id).where(IV.status.in_(("issued", "cancelled")))
    cond = _client_scope(principal, "invoice:read", IV.client_id)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _common(
        stmt,
        f,
        date_col=IV.doc_date,
        le_col=IV.legal_entity_id,
        client_col=IV.client_id,
        status_col=IV.status,
    )
    if f.q:
        stmt = stmt.where(func.upper(IV.number) == f.q.strip().upper())
    return lists._order(stmt, "invoices", f.sort, IV.id)


INVOICE_COLUMNS = (
    Column("number", "Invoice number", "string", IV.number),
    Column("doc_date", "Date", "date", IV.doc_date),
    Column("due_date", "Due", "date", IV.due_date),
    Column("client", "Client", "string", _client_name(IV.client_id)),
    Column("status", "Status", "enum", IV.status),
    Column("einvoice_status", "E-invoice", "enum", IV.einvoice_status),
    Column("payment_status", "Payment status", "enum", _payment_status(), sortable=False),
    Column("taxable_inr", "Taxable (INR)", "money", IV.inr_taxable_total, literal("INR"), finance=True),
    Column("total", "Total", "money", IV.document_total, IV.currency, finance=True),
    Column("total_inr", "Total (INR)", "money", IV.inr_document_total, literal("INR"), finance=True),
)


# ================================================================ payments
PM = T.payments.c


async def _payments(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "payments")
    stmt = select(PM.id)
    cond = _client_scope(principal, "payment:read", PM.client_id)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _common(stmt, f, date_col=PM.payment_date, le_col=PM.legal_entity_id, client_col=PM.client_id,
                   status_col=PM.allocation_status)  # fmt: skip
    return lists._order(stmt, "payments", f.sort, PM.id)


PAYMENT_COLUMNS = (
    Column("human_id", "Payment", "string", PM.human_id),
    Column("payment_date", "Date", "date", PM.payment_date),
    Column("client", "Client", "string", _client_name(PM.client_id)),
    Column("reference", "Reference", "string", PM.reference),
    Column("allocation_status", "Allocation", "enum", PM.allocation_status),
    Column("amount", "Amount", "money", PM.amount, PM.currency, finance=True),
    Column("inr_amount", "Amount (INR)", "money", PM.inr_amount, literal("INR"), finance=True),
)


# ================================================================ BGV cases
BG = T.bgv_cases.c


async def _bgv_cases(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "bgv_cases")
    stmt = _common(select(BG.id), f, status_col=BG.status)
    start, end = _date_range(f)
    if start is not None:
        stmt = stmt.where(func.date(BG.created_at) >= start)
    if end is not None:
        stmt = stmt.where(func.date(BG.created_at) <= end)
    return lists._order(stmt, "bgv_cases", f.sort, BG.id)


BGV_COLUMNS = (
    Column("human_id", "Case", "string", BG.human_id),
    Column("candidate", "Candidate", "string", _candidate_name(BG.candidate_id), pii=True),
    Column("status", "Status", "enum", BG.status),
    Column("created_at", "Created", "datetime", BG.created_at),
)


# ================================================================ vendors, submissions
V, VS = T.vendors.c, T.vendor_submissions.c


async def _vendors(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "vendors")
    stmt = _common(select(V.id), f, status_col=V.status)
    if f.q:
        stmt = stmt.where(V.legal_name.ilike(f"%{_escape(f.q.strip())}%"))
    return lists._order(stmt, "vendors", f.sort, V.id)


VENDOR_COLUMNS = (
    Column("human_id", "Vendor ID", "string", V.human_id),
    Column("legal_name", "Name", "string", V.legal_name),
    Column("status", "Status", "enum", V.status),
    Column("owner", "Owner", "string", _user_name(V.owner_id)),
    Column("created_at", "Created", "datetime", V.created_at),
)


async def _vendor_submissions(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "vendor_submissions")
    stmt = _common(select(VS.id), f, client_col=VS.client_id, status_col=VS.status, vendor_col=VS.vendor_id)
    start, end = _date_range(f)
    if start is not None:
        stmt = stmt.where(func.date(VS.created_at) >= start)
    if end is not None:
        stmt = stmt.where(func.date(VS.created_at) <= end)
    return lists._order(stmt, "vendor_submissions", f.sort, VS.id)


VENDOR_SUBMISSION_COLUMNS = (
    Column("human_id", "Submission", "string", VS.human_id),
    Column("candidate", "Candidate", "string", VS.full_name, pii=True),
    Column("vendor", "Vendor", "string", select(V.legal_name).where(V.id == VS.vendor_id).scalar_subquery()),
    Column("client", "Client", "string", _client_name(VS.client_id)),
    Column("status", "Status", "enum", VS.status),
    Column("vendor_rate", "Vendor rate", "money", VS.vendor_rate, VS.currency, compensation=True),
    Column("created_at", "Submitted", "datetime", VS.created_at),
)


# ================================================================ CRM
L, OP = T.leads.c, T.opportunities.c


async def _leads(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "leads")
    stmt = select(L.id).where(L.archived_at.is_(None))
    cond = scope_condition(principal, "lead:read", L.owner_id)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _common(stmt, f, status_col=L.status)
    return lists._order(stmt, "leads", f.sort, L.id)


LEAD_COLUMNS = (
    Column("human_id", "Lead", "string", L.human_id),
    Column("company_name", "Company", "string", L.company_name),
    Column("status", "Status", "enum", L.status),
    Column("source_code", "Source", "enum", L.source_code),
    Column("owner", "Owner", "string", _user_name(L.owner_id)),
    Column("created_at", "Created", "datetime", L.created_at),
)


async def _opportunities(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "opportunities")
    stmt = select(OP.id).where(OP.archived_at.is_(None))
    cond = scope_condition(principal, "opportunity:read", OP.owner_id)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _common(stmt, f, date_col=OP.expected_close_date, client_col=OP.client_id, status_col=OP.stage)
    return lists._order(stmt, "opportunities", f.sort, OP.id)


OPPORTUNITY_COLUMNS = (
    Column("human_id", "Opportunity", "string", OP.human_id),
    Column("name", "Name", "string", OP.name),
    Column("client", "Client", "string", _client_name(OP.client_id)),
    Column("stage", "Stage", "enum", OP.stage),
    Column("value", "Estimated revenue", "money", OP.estimated_revenue, OP.currency, compensation=True),
    Column("probability_pct", "Probability %", "decimal", OP.probability_pct),
    Column("expected_close_date", "Expected close", "date", OP.expected_close_date),
    Column("owner", "Owner", "string", _user_name(OP.owner_id)),
)


KINDS: tuple[ListKindDef, ...] = (
    ListKindDef("leads", P2ListFilters, LEAD_COLUMNS, _leads, "lead:read", L.id, "-created_at"),
    ListKindDef("opportunities", P2ListFilters, OPPORTUNITY_COLUMNS, _opportunities, "opportunity:read", OP.id, "expected_close_date"),
    ListKindDef("vendors", P2ListFilters, VENDOR_COLUMNS, _vendors, "vendor:read", V.id, "legal_name"),
    ListKindDef("vendor_submissions", P2ListFilters, VENDOR_SUBMISSION_COLUMNS, _vendor_submissions, "vendor:read", VS.id, "-created_at"),
    ListKindDef("placements", P2ListFilters, PLACEMENT_COLUMNS, _placements, "placement:read", P.id, "human_id"),
    ListKindDef("timesheets", P2ListFilters, TIMESHEET_COLUMNS, _timesheets, "timesheet:read", TS.id, "-period_start"),
    ListKindDef("expense_claims", P2ListFilters, EXPENSE_COLUMNS, _expense_claims, "expense:read", EC.id, "-submitted_at", finance_export=True),
    ListKindDef("invoices", P2ListFilters, INVOICE_COLUMNS, _invoices, perm("invoice:read"), IV.id, "-doc_date", finance_export=True),
    ListKindDef("payments", P2ListFilters, PAYMENT_COLUMNS, _payments, perm("payment:read"), PM.id, "-payment_date", finance_export=True),
    ListKindDef("bgv_cases", P2ListFilters, BGV_COLUMNS, _bgv_cases, "bgv:read", BG.id, "-created_at"),
)  # fmt: skip
P2_KINDS = tuple(k.kind for k in KINDS)

for _k in KINDS:
    register_list_kind(_k)
