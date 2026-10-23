# ruff: file-ignore[line-too-long, non-imperative-mood]
"""P1 / P2 report sources (Epic 47 story 47.1, FR-237, AD-33) -- the ONLY place they are registered.

Called from ``builder.hooks.register()`` at startup. Every field declares type, label, ``pii_class``, operators,
aggregates, grouping and date buckets; rates / CTC / margins / budgets are ``compensation`` (masked by the
foundation without ``compensation:view``). Free text (notes, feedback, JD text, email bodies) and identity
fields (PAN, Aadhaar, bank) are **not registered**. Posted-fact restrictions are ``base_filter``s:

* ``billing.invoice`` / ``billing.credit_note`` -- ``status = 'issued'`` only (drafts, pending approval,
  discarded and cancelled documents never appear);
* ``receivables.payment`` -- non-reversed payments only;
* ``time.timesheet`` -- entries of ``approved`` timesheets only.

Scope follows the owning domain: Demand-scoped sources (applications, submissions, interviews, offers) use the
Demand's owner / business unit through correlated scalar subqueries (only registered expressions, never text).
The base sources ``candidates.candidate`` and ``placements.worker`` are Epic 36's and are not registered here.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import ColumnElement, func, select

from app.core.semantic import (
    Agg,
    FieldDef,
    FieldType,
    JoinDef,
    Op,
    PiiClass,
    ScopeSpec,
    register_semantic_source,
    sources,
)
from app.models.billing import CreditNote, Invoice
from app.models.candidates import Candidate
from app.models.clients import Client
from app.models.demands import DEMAND_STATUSES, EMPLOYMENT_TYPES, PRIORITIES, WORK_MODES, Demand, DemandSkill
from app.models.interviews import INTERVIEW_MODES, INTERVIEW_STATUSES, Interview
from app.models.offers import OFFER_STATUSES, Offer, OfferVersion
from app.models.pipeline import (
    APPLICATION_SOURCES,
    FEEDBACK_STATUSES,
    SUBMISSION_STATUSES,
    Application,
    PipelineStage,
    Submission,
)
from app.models.placements import ENGAGEMENT_KINDS, PLACEMENT_STATUSES, WORKER_TYPES, Placement, RateSegment
from app.models.receivables import ALLOCATION_STATUSES, Allocation, Payment, TdsDeduction
from app.models.time import Timesheet, TimesheetEntry
from app.schemas.time import TIME_CATEGORIES

__all__ = ["FINANCE_SOURCES", "SOURCE_KEYS", "register"]

# Exports of these sources also need ``report:finance_export`` (A1).
FINANCE_SOURCES = frozenset({"billing.invoice", "billing.credit_note", "receivables.payment"})

_ID: frozenset[Op] = frozenset({"eq", "in"})
_UUID_OPS: frozenset[Op] = frozenset({"eq", "ne", "in", "not_in", "is_null"})
_ENUM_OPS: frozenset[Op] = frozenset({"eq", "ne", "in", "not_in"})
_STR_OPS: frozenset[Op] = frozenset({"eq", "in", "contains", "starts_with", "is_null"})
_NUM_OPS: frozenset[Op] = frozenset({"lt", "lte", "gt", "gte", "eq", "between", "is_null"})  # CMP + eq/between/is_null
_DATE_OPS: frozenset[Op] = frozenset({"lt", "lte", "gt", "gte", "between", "within_days", "is_null"})  # CMP + between/within_days/is_null
_NUM_AGGS: frozenset[Agg] = frozenset({"sum", "avg", "min", "max", "count"})
_DATE_AGGS: frozenset[Agg] = frozenset({"min", "max", "count"})

Expr = Callable[[Any], Any]


# ---------------------------------------------------------------- field builders
def _uuid(label: str, expr: Expr, *, groupable: bool = True, join: str | None = None) -> FieldDef:
    return FieldDef(
        "uuid",
        label,
        "none",
        expr,
        _UUID_OPS,
        aggs=frozenset({"count_distinct"}),
        groupable=groupable,
        join=join,
    )


def _ident(label: str, expr: Expr) -> FieldDef:
    return FieldDef("string", label, "none", expr, frozenset({"eq", "in", "starts_with"}))


def _enum(label: str, expr: Expr, values: tuple[str, ...], *, join: str | None = None) -> FieldDef:
    return FieldDef(
        "enum", label, "none", expr, _ENUM_OPS, aggs=frozenset({"count_distinct"}), groupable=True,
        enum_values=tuple(values), join=join,
    )  # fmt: skip


def _text(
    label: str, expr: Expr, *, pii: PiiClass = "none", join: str | None = None, groupable: bool = True
) -> FieldDef:
    return FieldDef(
        "string", label, pii, expr, _STR_OPS, aggs=frozenset({"count_distinct"}), groupable=groupable,
        join=join,
    )  # fmt: skip


def _date(label: str, expr: Expr, *, kind: FieldType = "date", join: str | None = None) -> FieldDef:
    return FieldDef(
        kind, label, "none", expr, _DATE_OPS, aggs=_DATE_AGGS, groupable=True, date_buckets=True, join=join
    )


def _num(
    label: str, expr: Expr, *, kind: FieldType = "decimal", pii: PiiClass = "none", join: str | None = None
) -> FieldDef:
    return FieldDef(kind, label, pii, expr, _NUM_OPS, aggs=_NUM_AGGS, join=join)


def _money(label: str, expr: Expr, *, join: str | None = None) -> FieldDef:
    """A rate / CTC / margin / budget: ``compensation`` (masked without ``compensation:view``)."""
    return _num(label, expr, pii="compensation", join=join)


def _bool(label: str, expr: Expr) -> FieldDef:
    return FieldDef("bool", label, "none", expr, frozenset({"eq"}), groupable=True)


def _client_join(client_id: Expr) -> JoinDef:
    return JoinDef(Client, lambda r, c: c.id == client_id(r))


def _client_name() -> FieldDef:
    return _text("Client", lambda c: c.name, join="client")


def _candidate_join(candidate_id: Expr) -> JoinDef:
    return JoinDef(Candidate, lambda r, c: c.id == candidate_id(r))


def _candidate_name() -> FieldDef:
    return _text("Candidate name", lambda c: c.full_name, pii="personal", join="candidate", groupable=False)


def _demand_join(demand_id: Expr) -> JoinDef:
    return JoinDef(Demand, lambda r, d: d.id == demand_id(r))


def _demand_fields() -> dict[str, FieldDef]:
    return {
        "demand.title": _text("Demand title", lambda d: d.title, join="demand"),
        "demand.req_id": FieldDef("string", "Demand number", "none", lambda d: d.req_id, frozenset({"eq", "in"}), join="demand", groupable=True),
        "demand.status": _enum("Demand status", lambda d: d.status, DEMAND_STATUSES, join="demand"),
    }  # fmt: skip


# ---------------------------------------------------------------- scope helpers (via the Demand)
def _demand_owner(demand_id: Expr) -> Expr:
    return lambda r: select(Demand.owner_id).where(Demand.id == demand_id(r)).scalar_subquery()


def _demand_bu(demand_id: Expr) -> Expr:
    return lambda r: select(Demand.business_unit_id).where(Demand.id == demand_id(r)).scalar_subquery()


def _client_owner(client_id: Expr) -> Expr:
    return lambda r: select(Client.owner_id).where(Client.id == client_id(r)).scalar_subquery()


def _placement_col(placement_id: Expr, column: str) -> Expr:
    return lambda r: (
        select(getattr(Placement, column)).where(Placement.id == placement_id(r)).scalar_subquery()
    )


def _current_rate(column: str) -> Expr:
    """The placement's latest active rate segment value: a correlated lookup on
    ``ix_rate_segments_placement_id (placement_id, effective_from)`` -- linear in the rows read (a join to a
    ``DISTINCT ON`` relation degrades to a quadratic nested loop when RLS predicates skew the row estimates)."""

    def expr(p: Any) -> ColumnElement[Any]:
        return (
            select(getattr(RateSegment, column))
            .where(RateSegment.placement_id == p.id, RateSegment.status == "active")
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
            .scalar_subquery()
        )

    return expr


# ================================================================ sources
def _demand() -> None:
    fields = {
        "id": FieldDef("uuid", "Demand id", "none", lambda d: d.id, _ID, aggs=frozenset({"count_distinct"})),
        "req_id": _ident("Demand number", lambda d: d.req_id),
        "title": _text("Title", lambda d: d.title),
        "client_id": _uuid("Client id", lambda d: d.client_id),
        "client.name": _client_name(),
        "status": _enum("Status", lambda d: d.status, DEMAND_STATUSES),
        "priority": _enum("Priority", lambda d: d.priority, PRIORITIES),
        "employment_type": _enum("Employment type", lambda d: d.employment_type, EMPLOYMENT_TYPES),
        "work_mode": _enum("Work mode", lambda d: d.work_mode, WORK_MODES),
        "country": _text("Country", lambda d: d.country),
        "location": _text("Location", lambda d: d.location),
        "positions": _num("Openings", lambda d: d.positions, kind="int"),
        "positions_filled": _num("Openings filled", lambda d: d.positions_filled, kind="int"),
        "applications_count": _num("Applications", lambda d: d.applications_count, kind="int"),
        "submissions_count": _num("Submissions", lambda d: d.submissions_count, kind="int"),
        "start_date": _date("Start date", lambda d: d.start_date),
        "target_closure_date": _date("Target closure", lambda d: d.target_closure_date),
        "opened_at": _date("Opened", lambda d: d.opened_at, kind="datetime"),
        "created_at": _date("Created", lambda d: d.created_at, kind="datetime"),
        "owner_id": _uuid("Owner", lambda d: d.owner_id),
        "business_unit_id": _uuid("Business unit", lambda d: d.business_unit_id),
        "currency": _text("Currency", lambda d: d.currency),
        "bill_rate": _money("Bill rate (budget)", lambda d: d.bill_rate),
        "pay_rate_min": _money("Pay rate min (budget)", lambda d: d.pay_rate_min),
        "pay_rate_max": _money("Pay rate max (budget)", lambda d: d.pay_rate_max),
        "is_archived": _bool("Archived", lambda d: d.archived_at.is_not(None)),
        "skills.skill_label": FieldDef(
            "string",
            "Skill",
            "none",
            lambda s: s.skill_label,
            frozenset({"eq", "in", "contains"}),
            join="skills",
        ),
        "skills.kind": FieldDef(
            "enum",
            "Skill kind",
            "none",
            lambda s: s.kind,
            frozenset({"eq", "in"}),
            enum_values=("mandatory", "preferred"),
            join="skills",
        ),
    }
    register_semantic_source(
        "demands.demand",
        Demand,
        fields,
        {
            "client": _client_join(lambda d: d.client_id),
            "skills": JoinDef(DemandSkill, lambda d, s: s.demand_id == d.id, many=True),
        },
        ScopeSpec(
            permission="demand:read", owner_cols=(lambda d: d.owner_id,), bu_col=lambda d: d.business_unit_id
        ),
        label="Demands",
        subject=lambda d: d.id,
    )


def _application() -> None:
    did: Expr = lambda a: a.demand_id  # ruff: ignore[lambda-assignment]
    fields = {
        "id": FieldDef(
            "uuid", "Application id", "none", lambda a: a.id, _ID, aggs=frozenset({"count_distinct"})
        ),
        "human_id": _ident("Application number", lambda a: a.human_id),
        "demand_id": _uuid("Demand id", lambda a: a.demand_id),
        "candidate_id": _uuid("Candidate id", lambda a: a.candidate_id),
        "stage.key": _text("Stage key", lambda s: s.key, join="stage"),
        "stage.label": _text("Stage", lambda s: s.label, join="stage"),
        "stage_entered_at": _date("Entered stage", lambda a: a.stage_entered_at, kind="datetime"),
        "source": _enum("Source", lambda a: a.source, APPLICATION_SOURCES),
        "owner_id": _uuid("Owner", lambda a: a.owner_id),
        "screening_passed": _bool("Screening passed", lambda a: a.screening_passed),
        "created_at": _date("Applied", lambda a: a.created_at, kind="datetime"),
        "is_archived": _bool("Archived", lambda a: a.archived_at.is_not(None)),
        "client.name": _client_name(),
        "candidate.full_name": _candidate_name(),
        **_demand_fields(),
    }
    register_semantic_source(
        "pipeline.application",
        Application,
        fields,
        {
            "stage": JoinDef(PipelineStage, lambda a, s: s.id == a.stage_id),
            "demand": _demand_join(did),
            "client": JoinDef(
                Client,
                lambda a, c: c.id
                == select(Demand.client_id).where(Demand.id == a.demand_id).scalar_subquery(),
            ),
            "candidate": _candidate_join(lambda a: a.candidate_id),
        },
        ScopeSpec(
            permission="demand:read",
            owner_cols=(_demand_owner(did), lambda a: a.owner_id),
            bu_col=_demand_bu(did),
        ),
        label="Applications",
        subject=lambda a: a.id,
    )


def _submission() -> None:
    did: Expr = lambda s: s.demand_id  # ruff: ignore[lambda-assignment]
    fields = {
        "id": FieldDef(
            "uuid", "Submission id", "none", lambda s: s.id, _ID, aggs=frozenset({"count_distinct"})
        ),
        "human_id": _ident("Submission number", lambda s: s.human_id),
        "status": _enum("Status", lambda s: s.status, SUBMISSION_STATUSES),
        "feedback_status": _enum("Client feedback", lambda s: s.feedback_status, FEEDBACK_STATUSES),
        "client_id": _uuid("Client id", lambda s: s.client_id),
        "demand_id": _uuid("Demand id", lambda s: s.demand_id),
        "candidate_id": _uuid("Candidate id", lambda s: s.candidate_id),
        "created_at": _date("Created", lambda s: s.created_at, kind="datetime"),
        "sent_at": _date("Sent", lambda s: s.sent_at, kind="datetime"),
        "currency": _text("Currency", lambda s: s.currency),
        "employment_type": _text("Employment type", lambda s: s.employment_type),
        "bill_rate": _money("Bill rate", lambda s: s.bill_rate),
        "pay_rate": _money("Pay rate", lambda s: s.pay_rate),
        "margin_amount": _money("Margin", lambda s: s.margin_amount),
        "margin_pct": _money("Margin %", lambda s: s.margin_pct),
        "below_min_margin": _bool("Below minimum margin", lambda s: s.below_min_margin),
        "client.name": _client_name(),
        "candidate.full_name": _candidate_name(),
        **_demand_fields(),
    }
    register_semantic_source(
        "pipeline.submission",
        Submission,
        fields,
        {
            "demand": _demand_join(did),
            "client": _client_join(lambda s: s.client_id),
            "candidate": _candidate_join(lambda s: s.candidate_id),
        },
        ScopeSpec(permission="submission:read", owner_cols=(_demand_owner(did),), bu_col=_demand_bu(did)),
        label="Submissions",
        subject=lambda s: s.id,
    )


def _interview() -> None:
    did: Expr = lambda i: i.demand_id  # ruff: ignore[lambda-assignment]
    fields = {
        "id": FieldDef(
            "uuid", "Interview id", "none", lambda i: i.id, _ID, aggs=frozenset({"count_distinct"})
        ),
        "human_id": _ident("Interview number", lambda i: i.human_id),
        "status": _enum("Status", lambda i: i.status, INTERVIEW_STATUSES),
        "mode": _enum("Mode", lambda i: i.mode, INTERVIEW_MODES),
        "stage_name": _text("Interview stage", lambda i: i.stage_name),
        "round": _num("Round", lambda i: i.round, kind="int"),
        "starts_at": _date("Scheduled for", lambda i: i.starts_at, kind="datetime"),
        "completed_at": _date("Completed", lambda i: i.completed_at, kind="datetime"),
        "organizer_user_id": _uuid("Organizer", lambda i: i.organizer_user_id),
        "demand_id": _uuid("Demand id", lambda i: i.demand_id),
        "candidate_id": _uuid("Candidate id", lambda i: i.candidate_id),
        "candidate.full_name": _candidate_name(),
        **_demand_fields(),
    }
    register_semantic_source(
        "interviews.interview",
        Interview,
        fields,
        {"demand": _demand_join(did), "candidate": _candidate_join(lambda i: i.candidate_id)},
        ScopeSpec(permission="interview:read", owner_cols=(_demand_owner(did),), bu_col=_demand_bu(did)),
        label="Interviews",
        subject=lambda i: i.id,
    )


def _offer() -> None:
    did: Expr = lambda o: o.demand_id  # ruff: ignore[lambda-assignment]
    fields = {
        "id": FieldDef("uuid", "Offer id", "none", lambda o: o.id, _ID, aggs=frozenset({"count_distinct"})),
        "human_id": _ident("Offer number", lambda o: o.human_id),
        "status": _enum("Status", lambda o: o.status, OFFER_STATUSES),
        "client_id": _uuid("Client id", lambda o: o.client_id),
        "demand_id": _uuid("Demand id", lambda o: o.demand_id),
        "candidate_id": _uuid("Candidate id", lambda o: o.candidate_id),
        "owner_id": _uuid("Owner", lambda o: o.owner_id),
        "created_at": _date("Created", lambda o: o.created_at, kind="datetime"),
        "released_at": _date("Released", lambda o: o.released_at, kind="datetime"),
        "responded_at": _date("Responded", lambda o: o.responded_at, kind="datetime"),
        "expires_at": _date("Expires", lambda o: o.expires_at, kind="datetime"),
        "version.pay_rate": _money("Offered pay (CTC)", lambda v: v.pay_rate, join="version"),
        "version.bill_rate": _money("Bill rate", lambda v: v.bill_rate, join="version"),
        "version.margin_pct": _money("Margin %", lambda v: v.margin_pct, join="version"),
        "client.name": _client_name(),
        "candidate.full_name": _candidate_name(),
        **_demand_fields(),
    }
    register_semantic_source(
        "offers.offer",
        Offer,
        fields,
        {
            "version": JoinDef(OfferVersion, lambda o, v: v.id == o.current_version_id),
            "demand": _demand_join(did),
            "client": _client_join(lambda o: o.client_id),
            "candidate": _candidate_join(lambda o: o.candidate_id),
        },
        ScopeSpec(
            permission="offer:read",
            owner_cols=(_demand_owner(did), lambda o: o.owner_id),
            bu_col=_demand_bu(did),
        ),
        label="Offers",
        subject=lambda o: o.id,
    )


def _placement() -> None:
    fields = {
        "id": FieldDef(
            "uuid", "Placement id", "none", lambda p: p.id, _ID, aggs=frozenset({"count_distinct"})
        ),
        "human_id": _ident("Placement number", lambda p: p.human_id),
        "status": _enum("Status", lambda p: p.status, PLACEMENT_STATUSES),
        "worker_type": _enum("Worker type", lambda p: p.worker_type, WORKER_TYPES),
        "engagement_kind": _enum("Engagement", lambda p: p.engagement_kind, ENGAGEMENT_KINDS),
        "client_id": _uuid("Client id", lambda p: p.client_id),
        "candidate_id": _uuid("Worker id", lambda p: p.candidate_id),
        "legal_entity_id": _uuid("Legal entity", lambda p: p.legal_entity_id),
        "account_manager_user_id": _uuid("Account manager", lambda p: p.account_manager_user_id),
        "recruiter_user_id": _uuid("Recruiter", lambda p: p.recruiter_user_id),
        "planned_start_date": _date("Planned start", lambda p: p.planned_start_date),
        "actual_start_date": _date("Actual start", lambda p: p.actual_start_date),
        "end_date": _date("End date", lambda p: func.coalesce(p.end_date, p.planned_end_date)),
        "created_at": _date("Created", lambda p: p.created_at, kind="datetime"),
        "role_title": _text("Role", lambda p: p.role_title),
        "currency": _text("Currency", lambda p: p.currency),
        "bill_rate": _money("Bill rate", _current_rate("bill_rate")),
        "pay_rate": _money("Pay rate", _current_rate("pay_rate")),
        "margin_amount": _money("Margin", _current_rate("margin_amount")),
        "margin_pct": _money("Margin %", _current_rate("margin_pct")),
        "client.name": _client_name(),
        "candidate.full_name": _candidate_name(),
        **_demand_fields(),
    }
    register_semantic_source(
        "placements.placement",
        Placement,
        fields,
        {
            "client": _client_join(lambda p: p.client_id),
            "candidate": _candidate_join(lambda p: p.candidate_id),
            "demand": _demand_join(lambda p: p.demand_id),
        },
        ScopeSpec(
            permission="placement:read",
            owner_cols=(lambda p: p.recruiter_user_id, lambda p: p.account_manager_user_id),
        ),
        label="Placements",
        subject=lambda p: p.id,
    )


def _timesheet() -> None:
    tid: Expr = lambda e: e.timesheet_id  # ruff: ignore[lambda-assignment]

    def ts(column: str) -> Expr:
        return (
            lambda e: select(getattr(Timesheet, column))
            .where(Timesheet.id == e.timesheet_id)
            .scalar_subquery()
        )

    approved = select(Timesheet.id).where(Timesheet.status == "approved")
    fields = {
        "timesheet_id": _uuid("Timesheet id", lambda e: e.timesheet_id),
        "entry_date": _date("Date", lambda e: e.entry_date),
        "category": _enum("Category", lambda e: e.category, TIME_CATEGORIES),
        "hours": _num("Hours", lambda e: e.hours),
        "timesheet.period_start": _date("Period start", lambda t: t.period_start, join="timesheet"),
        "timesheet.period_end": _date("Period end", lambda t: t.period_end, join="timesheet"),
        "timesheet.kind": _text("Timesheet kind", lambda t: t.kind, join="timesheet"),
        "timesheet.placement_id": _uuid("Placement id", lambda t: t.placement_id, join="timesheet"),
        "timesheet.client_id": _uuid("Client id", lambda t: t.client_id, join="timesheet"),
        "timesheet.approved_at": _date(
            "Approved", lambda t: t.approved_at, kind="datetime", join="timesheet"
        ),
        "client.name": _client_name(),
        "candidate.full_name": _candidate_name(),
    }
    placement_id = ts("placement_id")
    register_semantic_source(
        "time.timesheet",
        TimesheetEntry,
        fields,
        {
            "timesheet": JoinDef(Timesheet, lambda e, t: t.id == e.timesheet_id),
            "client": JoinDef(Client, lambda e, c: c.id == ts("client_id")(e)),
            "candidate": JoinDef(Candidate, lambda e, c: c.id == ts("candidate_id")(e)),
        },
        ScopeSpec(
            permission="timesheet:read",
            owner_cols=(
                _placement_col(placement_id, "recruiter_user_id"),
                _placement_col(placement_id, "account_manager_user_id"),
            ),
        ),
        label="Approved timesheet hours",
        subject=tid,
        base_filter=lambda e: e.timesheet_id.in_(approved),
    )


def _billing_doc(key: str, model: Any, label: str, extra: dict[str, FieldDef]) -> None:
    fields = {
        "id": FieldDef(
            "uuid", "Document id", "none", lambda d: d.id, _ID, aggs=frozenset({"count_distinct"})
        ),
        "number": _ident("Number", lambda d: d.number),
        "fiscal_year": _text("Financial year", lambda d: d.fiscal_year),
        "client_id": _uuid("Client id", lambda d: d.client_id),
        "legal_entity_id": _uuid("Legal entity", lambda d: d.legal_entity_id),
        "doc_date": _date("Document date", lambda d: d.doc_date),
        "issued_at": _date("Issued", lambda d: d.issued_at, kind="datetime"),
        "currency": _text("Currency", lambda d: d.currency),
        "taxable_total": _num("Taxable value", lambda d: d.taxable_total),
        "tax_total": _num("Tax", lambda d: d.charged_tax_total),
        "document_total": _num("Total", lambda d: d.document_total),
        "inr_taxable_total": _num("Taxable value (INR)", lambda d: d.inr_taxable_total),
        "inr_document_total": _num("Total (INR)", lambda d: d.inr_document_total),
        "client.name": _client_name(),
        **extra,
    }
    register_semantic_source(
        key,
        model,
        fields,
        {"client": _client_join(lambda d: d.client_id)},
        ScopeSpec(permission="billing:read", owner_cols=(_client_owner(lambda d: d.client_id),)),
        label=label,
        subject=lambda d: d.id,
        base_filter=lambda d: d.status == "issued",
    )


def _invoice() -> None:
    _billing_doc(
        "billing.invoice", Invoice, "Issued invoices", {"due_date": _date("Due date", lambda d: d.due_date)}
    )


def _credit_note() -> None:
    _billing_doc(
        "billing.credit_note",
        CreditNote,
        "Issued credit notes",
        {
            "reason": _text("Reason", lambda d: d.reason),
            "original_invoice_id": _uuid(
                "Original invoice", lambda d: d.original_invoice_id, groupable=False
            ),
        },
    )


def _payment() -> None:
    tds = lambda p: (  # ruff: ignore[lambda-assignment]
        select(func.coalesce(func.sum(TdsDeduction.inr_amount), 0))
        .where(TdsDeduction.payment_id == p.id)
        .scalar_subquery()
    )
    fields = {
        "id": FieldDef("uuid", "Payment id", "none", lambda p: p.id, _ID, aggs=frozenset({"count_distinct"})),
        "human_id": _ident("Payment number", lambda p: p.human_id),
        "client_id": _uuid("Client id", lambda p: p.client_id),
        "legal_entity_id": _uuid("Legal entity", lambda p: p.legal_entity_id),
        "payment_date": _date("Payment date", lambda p: p.payment_date),
        "amount": _num("Amount", lambda p: p.amount),
        "inr_amount": _num("Amount (INR)", lambda p: p.inr_amount),
        "currency": _text("Currency", lambda p: p.currency),
        "mode": _text("Mode", lambda p: p.mode),
        "allocation_status": _enum("Allocation", lambda p: p.allocation_status, ALLOCATION_STATUSES),
        "tds_amount": _num("TDS (INR)", tds),
        "client.name": _client_name(),
        "allocations.invoice_id": FieldDef(
            "uuid", "Allocated to invoice", "none", lambda a: a.invoice_id, _ID, join="allocations"
        ),
    }
    register_semantic_source(
        "receivables.payment",
        Payment,
        fields,
        {
            "client": _client_join(lambda p: p.client_id),
            "allocations": JoinDef(Allocation, lambda p, a: a.payment_id == p.id, many=True),
        },
        ScopeSpec(permission="receivables:read", owner_cols=(_client_owner(lambda p: p.client_id),)),
        label="Payments received",
        subject=lambda p: p.id,
        base_filter=lambda p: p.reversed_at.is_(None) & (p.allocation_status != "reversed"),
    )


_REGISTRARS: dict[str, Callable[[], None]] = {
    "demands.demand": _demand,
    "pipeline.application": _application,
    "pipeline.submission": _submission,
    "interviews.interview": _interview,
    "offers.offer": _offer,
    "placements.placement": _placement,
    "time.timesheet": _timesheet,
    "billing.invoice": _invoice,
    "billing.credit_note": _credit_note,
    "receivables.payment": _payment,
}
SOURCE_KEYS: tuple[str, ...] = tuple(_REGISTRARS)


def register() -> None:
    """Register the P1 / P2 report sources once per process (safe to call again)."""
    registered = sources()
    for key, registrar in _REGISTRARS.items():
        if key not in registered:
            registrar()
