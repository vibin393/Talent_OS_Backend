# ruff: noqa: E501, N812
"""Phase 2 dashboards (Epic 34, A1/A3): finance, account_ops, onboarding, compliance, delivery, executive.

Registered into the P1 ``DASHBOARDS`` registry (same tile mechanics: independent tiles, Redis cache, statement
timeout, drill-down links, ``unavailable``). Availability is decided by **permissions**, not role names
(``P2_GATES``); a Phase 2 default role's own dashboard is listed first (FR-81).
"""

from typing import Any

from app.core.permissions import Principal
from app.services.reporting import p2tables as _T
from app.services.reporting.dashboards import DashboardDef, TileDef, TileFn
from app.services.reporting.metrics import account_ops as _AO
from app.services.reporting.metrics import compliance as _CO
from app.services.reporting.metrics import delivery as _DL
from app.services.reporting.metrics import executive as _EX
from app.services.reporting.metrics import finance as _FI
from app.services.reporting.metrics import onboarding as _ON

_need = _T.needs
_BILL = _need(*_T.BILLING)


def _p2(key: str, title: str, kind: str, size: str, desc: str, fn: TileFn, **kw: Any) -> TileDef:
    return TileDef(key, title, kind, size, desc, fn, **kw)


P2_DASHBOARDS: dict[str, DashboardDef] = {
    "finance": DashboardDef("finance", "Finance", (
        _p2("wip_unbilled_by_client", "Approved, not yet invoiced", "table", "l",
            "Estimate: approved timesheets not yet invoiced, by client (from the timesheet monitor).",
            _FI.wip_unbilled_by_client, requires=_need("timesheets"), permissions=("timesheet:read",),
            compensation=True, estimate=True),
        _p2("invoices_issued", "Invoices issued", "kpi", "s",
            "Issued invoices dated in range (ex-GST, INR), excluding IRN-cancelled and imported invoices.",
            _FI.invoices_issued, requires=_BILL, permissions=("invoice:read",)),
        _p2("invoices_pending_irn", "Pending IRN", "kpi", "s",
            "Invoices awaiting an e-invoice IRN; oldest age and those due within 7 days.",
            _FI.invoices_pending_irn, requires=_need("invoices"), permissions=("invoice:read",)),
        _p2("ar_aging", "AR aging", "breakdown", "m",
            "Open receivables by aging bucket as of the selected date; disputed amounts shown separately.",
            _FI.ar_aging, permissions=("collections:read",)),
        _p2("dso_trend", "DSO trend", "series", "m",
            "Days sales outstanding per legal entity over the last 12 months.",
            _FI.dso_trend, permissions=("collections:read",)),
        _p2("collections", "Collections", "kpi", "s",
            "Payments received in range net of reversals (INR).",
            _FI.collections, requires=_need("payments"), permissions=("payment:read",)),
        _p2("promises_overdue", "Promises to pay overdue", "table", "m",
            "Promises to pay whose date has passed without being kept.",
            _FI.promises_overdue, requires=_need("collection_activities"), permissions=("collections:read",)),
        _p2("disputes_open", "Open disputes", "kpi", "s", "Open invoice disputes: count, amount, median age.",
            _FI.disputes_open, requires=_need("disputes"), permissions=("collections:read",)),
        _p2("unallocated_payments", "Unallocated payments", "kpi", "s",
            "Client credit from payments not yet allocated, as of the selected date.",
            _FI.unallocated_payments, requires=_need("client_credits"), permissions=("payment:read",)),
        _p2("tds_certificates_outstanding", "TDS certificates outstanding", "kpi", "s",
            "TDS deductions without a received certificate.",
            _FI.tds_certificates_outstanding, requires=_need("tds_deductions"), permissions=("collections:read",)),
        _p2("missing_timesheets_value", "Missing timesheets", "table", "m",
            "Estimate: timesheets past their grace period and not submitted, with their estimated value.",
            _FI.missing_timesheets_value, requires=_need("timesheets"), permissions=("timesheet:read",),
            compensation=True, estimate=True),
        _p2("payroll_export_status", "Payroll export status", "table", "m",
            "Pay periods by status.", _FI.payroll_export_status, requires=_need("pay_periods"),
            permissions=("payroll:read",)),
    )),
    "account_ops": DashboardDef("account_ops", "Accounts", (
        _p2("active_placements_by_client", "Active placements by client", "table", "m",
            "Active placements you can see, by client.", _AO.active_placements_by_client,
            requires=_need("placements"), permissions=("placement:read",)),
        _p2("placements_ending_30_60", "Placements ending", "kpi", "s",
            "Active placements whose planned end falls in the next 30 / 60 days.",
            _AO.placements_ending_30_60, requires=_need("placements"), permissions=("placement:read",)),
        _p2("extensions_in_progress", "Extensions in progress", "kpi", "s", "Open placement extensions.",
            _AO.extensions_in_progress, requires=_need("placements", "placement_extensions"),
            permissions=("placement:read",)),
        _p2("po_near_limit", "POs near limit", "table", "m",
            "Active POs with less than 15% balance left or expiring within 30 days.",
            _AO.po_near_limit, requires=_need("purchase_orders", "po_consumptions"), permissions=("placement:read",)),
        _p2("timesheets_pending_client", "Timesheets awaiting approval", "kpi", "s",
            "Submitted timesheets awaiting approval on your placements.", _AO.timesheets_pending_client,
            requires=_need("timesheets", "placements"), permissions=("timesheet:read",)),
        _p2("revenue_margin_by_client_month", "Revenue and margin by client", "table", "l",
            "Invoiced revenue (ex-GST) and gross margin by client and month, from issued documents.",
            _AO.revenue_margin_by_client_month, requires=_BILL, permissions=("invoice:read",), compensation=True),
    )),
    "onboarding": DashboardDef("onboarding", "Onboarding", (
        _p2("prejoining_by_days_to_start", "Pre-joining by days to start", "breakdown", "m",
            "Pre-joining placements by days until the planned start.", _ON.prejoining_by_days_to_start,
            requires=_need("placements"), permissions=("placement:read",)),
        _p2("dropout_risk", "Dropout risk", "table", "m", "Pre-joining placements flagged as dropout risks.",
            _ON.dropout_risk, requires=_need("placements"), permissions=("placement:read",)),
        _p2("onboarding_cases_by_completion", "Onboarding progress", "breakdown", "m",
            "Open onboarding cases by completion quartile.", _ON.onboarding_cases_by_completion,
            requires=_need("onboarding_cases", "placements"), permissions=("onboarding:read",)),
        _p2("start_gates_unmet_t3", "Start gates unmet (T-3)", "table", "m",
            "Placements starting within 3 days with required start gates still pending.",
            _ON.start_gates_unmet_t3, requires=_need("placements", "start_gates"), permissions=("placement:read",)),
    )),
    "compliance": DashboardDef("compliance", "Compliance", (
        _p2("bgv_by_status_age", "BGV cases by status and age", "breakdown", "m",
            "Open BGV cases by status, split by age.", _CO.bgv_by_status_age, requires=_need("bgv_cases"),
            permissions=("bgv:read",)),
        _p2("discrepancies_awaiting_response", "Discrepancies awaiting response", "kpi", "s",
            "BGV discrepancies awaiting the candidate response.", _CO.discrepancies_awaiting_response,
            requires=_need("bgv_discrepancies"), permissions=("bgv:read",)),
        _p2("credentials_expiring", "Credentials expiring", "kpi", "m",
            "Current credentials expiring within 30 / 60 / 90 days.", _CO.credentials_expiring,
            requires=_need("credentials"), permissions=("credential:read",)),
        _p2("noncompliant_placements", "Non-compliant placements", "kpi", "s",
            "Placements blocked by compliance.", _CO.noncompliant_placements, requires=_need("placements"),
            permissions=("placement:read",)),
        _p2("sla_bgv_turnaround", "BGV turnaround SLA", "breakdown", "m",
            "BGV SLA clocks started in range, by status.", _CO.sla_bgv_turnaround, requires=_need("sla_clocks"),
            permissions=("bgv:read",)),
    )),
    "delivery": DashboardDef("delivery", "Delivery", (
        _p2("bench_summary", "Bench", "kpi", "m", "Workers on bench, bench days and bench cost.",
            _DL.bench_summary, requires=_need("bench_entries"), permissions=("placement:read",), compensation=True),
        _p2("vendor_performance_summary", "Vendor performance", "table", "l",
            "Top and bottom vendors by acceptance rate in range, with SLA breaches and early terminations.",
            _DL.vendor_performance_summary, requires=_need("vendors", "vendor_submissions"),
            permissions=("vendor:read",)),
        _p2("early_terminations", "Early terminations", "table", "m",
            "Placements ended within 30 days of start, by source vendor / recruiter.", _DL.early_terminations,
            requires=_need("placements"), permissions=("placement:read",)),
        _p2("roll_offs_30_60", "Roll-offs", "kpi", "s", "Active placements ending in the next 30 / 60 days.",
            _DL.roll_offs_30_60, requires=_need("placements"), permissions=("placement:read",)),
    )),
    "executive": DashboardDef("executive", "Executive", (
        _p2("exec_grid", "Executive summary", "table", "l",
            "Monthly revenue, margin, workers on billing, placements, fill rate (assumption), pipeline, DSO and "
            "overdue AR per legal entity, from reconciled summaries.", _EX.exec_grid,
            requires=_need("legal_entities"), permissions=("executive:read",)),
        _p2("revenue_margin_trend", "Revenue and margin", "series", "m",
            "Monthly revenue (ex-GST) and gross margin.", _EX.revenue_margin_trend, requires=_need("legal_entities"),
            permissions=("executive:read",)),
        _p2("ar_overdue_trend", "Overdue AR", "series", "m", "Overdue receivables at each month end.",
            _EX.ar_overdue_trend, requires=_need("legal_entities"), permissions=("executive:read",)),
    )),
}  # fmt: skip

P2_GATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "finance": (("invoice:read", "collections:read"),),
    "account_ops": (("placement:read",),),
    "onboarding": (("onboarding:read",),),
    "compliance": (("bgv:read",), ("credential:read",)),
    "delivery": (("placement:read", "vendor:read"),),
    "executive": (("executive:read",),),
}
P2_ORDER = ("finance", "account_ops", "onboarding", "compliance", "delivery", "executive")
P2_ROLE_DEFAULT: dict[str, str] = {
    "finance": "finance",
    "finance_admin": "finance",
    "account_manager": "account_ops",
    "hr_onboarding": "onboarding",
    "compliance": "compliance",
    "delivery_manager": "delivery",
}


def p2_allowed(principal: Principal, key: str) -> bool:
    from app.services.reporting.filters import holds

    return any(all(holds(principal, p) for p in group) for group in P2_GATES[key])


def p2_keys(principal: Principal, role_keys: set[str], is_admin: bool) -> tuple[list[str], list[str]]:
    """(dashboards to list first -- the caller's P2 default roles, the other allowed P2 dashboards)."""
    allowed = [k for k in P2_ORDER if p2_allowed(principal, k)]
    first = (
        []
        if is_admin
        else list(dict.fromkeys(P2_ROLE_DEFAULT[r] for r in role_keys if P2_ROLE_DEFAULT.get(r) in allowed))
    )
    return first, allowed
