# ruff: noqa: E501
"""Phase 2 read-model projections (Epic 34, AD-1 read-model exception; see ``common``).

Every Phase 2 table reporting reads is a lightweight ``sqlalchemy.table()`` projection registered in
``common.OPTIONAL_TABLES`` with the columns the SQL needs. ``available()`` checks them once per process, so a
table of an epic that has not merged yet (AR, payroll, extensions...) hides its tiles (``unavailable``) instead
of failing. Reporting never writes to any of these tables.
"""

from collections.abc import Callable

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Uuid, column, table
from sqlalchemy.sql.expression import TableClause

from app.services.reporting import common

_TYPES = {
    "u": Uuid(),
    "s": String(),
    "d": Date(),
    "t": DateTime(timezone=True),
    "n": Numeric(),
    "i": Integer(),
    "b": Boolean(),
}


def _t(name: str, spec: str) -> TableClause:
    """``_t("x", "id:u status:s")`` -> ``table("x", column("id", Uuid()), column("status", String()))``."""
    cols = []
    for part in spec.split():
        col, kind = part.split(":")
        cols.append(column(col, _TYPES[kind]))
    return table(name, *cols)


def _required(tbl: TableClause) -> tuple[str, ...]:
    return tuple(c.name for c in tbl.columns)


# ---------------------------------------------------------------- foundation / hubs
legal_entities = _t("legal_entities", "id:u name:s status:s timezone:s")
placements = _t(
    "placements",
    "id:u human_id:s candidate_id:u client_id:u vendor_id:u legal_entity_id:u demand_id:u status:s "
    "planned_start_date:d actual_start_date:d end_date:d planned_end_date:d account_manager_user_id:u "
    "recruiter_user_id:u dropout_risk:b compliance_blocked:b onboarding_pct:i po_id:u currency:s "
    "role_title:s created_at:t",
)
rate_segments = _t(
    "rate_segments",
    "placement_id:u effective_from:d effective_to:d status:s pay_rate:n bill_rate:n vendor_rate:n "
    "margin_amount:n margin_pct:n currency:s rate_unit:s",
)
start_gates = _t("start_gates", "id:u placement_id:u kind:s required:b status:s")

# ---------------------------------------------------------------- billing (Epic 29)
invoices = _t(
    "invoices",
    "id:u legal_entity_id:u client_id:u number:s doc_date:d due_date:d status:s is_imported:b cancel_basis:s "
    "currency:s document_total:n inr_taxable_total:n inr_document_total:n einvoice_status:s irn_report_by:d "
    "issued_at:t",
)
invoice_lines = _t("invoice_lines", "id:u invoice_id:u placement_id:u inr_taxable_value:n cost_amount_inr:n")
credit_notes = _t(
    "credit_notes",
    "id:u legal_entity_id:u client_id:u number:s doc_date:d status:s original_invoice_id:u inr_document_total:n",
)
credit_note_lines = _t(
    "credit_note_lines", "id:u credit_note_id:u placement_id:u inr_taxable_value:n cost_amount_inr:n"
)
debit_notes = _t(
    "debit_notes",
    "id:u legal_entity_id:u client_id:u number:s doc_date:d status:s cancel_basis:s original_invoice_id:u "
    "inr_document_total:n",
)
debit_note_lines = _t(
    "debit_note_lines", "id:u debit_note_id:u placement_id:u inr_taxable_value:n cost_amount_inr:n"
)

# ---------------------------------------------------------------- receivables (Epic 30, in flight)
payments = _t(
    "payments",
    "id:u human_id:s legal_entity_id:u client_id:u payment_date:d amount:n currency:s inr_amount:n "
    "allocation_status:s reversal_date:d reference:s",
)
client_credits = _t(
    "client_credits", "client_id:u legal_entity_id:u credit_date:d kind:s amount:n payment_id:u"
)
collection_activities = _t(
    "collection_activities",
    "id:u client_id:u document_id:u kind:s ptp_amount:n ptp_currency:s ptp_date:d ptp_status:s owner_id:u",
)
disputes = _t("disputes", "id:u amount:n status:s opened_at:t")
tds_deductions = _t("tds_deductions", "id:u legal_entity_id:u amount:n deduction_date:d certificate_status:s")
pay_periods = _t("pay_periods", "id:u human_id:s legal_entity_id:u period_start:d period_end:d status:s")
payroll_export_versions = _t(
    "payroll_export_versions", "pay_period_id:u kind:s seq:i status:s finalized_at:t"
)

# ---------------------------------------------------------------- commercials, time, expenses
purchase_orders = _t(
    "purchase_orders",
    "id:u client_id:u legal_entity_id:u po_number:s currency:s value_cap:n valid_to:d status:s",
)
po_consumptions = _t("po_consumptions", "po_id:u amount:n")
timesheets = _t(
    "timesheets",
    "id:u human_id:s placement_id:u candidate_id:u client_id:u legal_entity_id:u kind:s status:s "
    "period_start:d period_end:d submitted_at:t approved_at:t",
)
expense_claims = _t(
    "expense_claims",
    "id:u human_id:s placement_id:u candidate_id:u client_id:u legal_entity_id:u currency:s title:s status:s "
    "total_claimed:n total_approved:n submitted_at:t",
)
placement_extensions = _t("placement_extensions", "id:u placement_id:u status:s")

# ---------------------------------------------------------------- workforce / compliance
onboarding_cases = _t("onboarding_cases", "id:u placement_id:u status:s progress_pct:i")
bgv_cases = _t("bgv_cases", "id:u human_id:s candidate_id:u placement_id:u status:s created_at:t")
bgv_discrepancies = _t("bgv_discrepancies", "id:u case_id:u status:s response_due_on:d")
credentials = _t(
    "credentials",
    "id:u candidate_id:u type_key:s expires_on:d verification_status:s superseded_at:t erased_at:t",
)
sla_clocks = _t(
    "sla_clocks",
    "subject_type:s milestone:s status:s started_at:t completed_at:t due_at:t org_vendor_id:u breached_at:t",
)
bench_entries = _t(
    "bench_entries",
    "id:u candidate_id:u legal_entity_id:u status:s available_from:d cost_per_day:n cost_currency:s",
)

# ---------------------------------------------------------------- vendors / CRM
vendors = _t("vendors", "id:u human_id:s legal_name:s status:s owner_id:u created_at:t")
vendor_submissions = _t(
    "vendor_submissions",
    "id:u human_id:s vendor_id:u demand_id:u client_id:u full_name:s status:s vendor_rate:n currency:s "
    "placement_id:u created_at:t",
)
opportunities = _t(
    "opportunities",
    "id:u human_id:s name:s client_id:u stage:s estimated_revenue:n probability_pct:n currency:s "
    "expected_close_date:d owner_id:u archived_at:t created_at:t",
)
leads = _t(
    "leads", "id:u human_id:s company_name:s status:s source_code:s owner_id:u created_at:t archived_at:t"
)

P2_TABLES: tuple[TableClause, ...] = (
    legal_entities,
    placements,
    rate_segments,
    start_gates,
    invoices,
    invoice_lines,
    credit_notes,
    credit_note_lines,
    debit_notes,
    debit_note_lines,
    payments,
    client_credits,
    collection_activities,
    disputes,
    tds_deductions,
    pay_periods,
    payroll_export_versions,
    purchase_orders,
    po_consumptions,
    timesheets,
    expense_claims,
    placement_extensions,
    onboarding_cases,
    bgv_cases,
    bgv_discrepancies,
    credentials,
    sla_clocks,
    bench_entries,
    vendors,
    vendor_submissions,
    opportunities,
    leads,
)
for _tbl in P2_TABLES:
    common.OPTIONAL_TABLES.setdefault(_tbl.name, _required(_tbl))

BILLING = (
    "invoices",
    "invoice_lines",
    "credit_notes",
    "credit_note_lines",
    "debit_notes",
    "debit_note_lines",
)


def needs(*names: str) -> Callable[[frozenset[str]], bool]:
    """Tile requirement: every named optional table is present (with its columns)."""
    wanted = frozenset(names)

    def check(avail: frozenset[str]) -> bool:
        return wanted <= avail

    check.tables = wanted  # type: ignore[attr-defined]
    return check
