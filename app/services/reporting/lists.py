"""List views (Epic 11, A4): the single source of both ``GET /reports/lists/{kind}`` and the export engine,
so exported rows are exactly the listed rows.

* ``COLUMNS[kind]`` -- ``Column(key, label, type, expr, currency, compensation, pii)``. Lookups (names,
  labels) are correlated scalar subqueries, so every query stays ``FROM <entity>`` with one row per entity.
* ``build(ctx, db, kind, filters, ...)`` returns a ``ListQuery``: the scoped + filtered + ordered id query
  (``base``), the row query (id + selected columns) and the masking outcome.
* Candidates and demands **reuse** their domain's ``list_query`` (the same statement ``GET /candidates`` /
  ``GET /demands`` page through). Applications, submissions, interviews and offers are built here, scoped
  through the demand (``demands.access_condition`` with the kind's read permission) like their domains do.
* Compensation columns: masked (``None``, ``masked=True``) in list responses and **omitted** from exports
  for principals without ``compensation:view`` (AD-11).
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.pagination import PageParams
from app.core.permissions import Principal, can_view_compensation, check
from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
from app.models.clients import Client
from app.models.demands import Demand
from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage, Submission
from app.schemas.reporting import (
    ApplicationListFilters,
    ColumnOut,
    InterviewListFilters,
    OfferListFilters,
    SubmissionListFilters,
    split_csv,
)
from app.services.reporting.common import (
    available,
    business_days_ago,
    business_units,
    day_range,
    demand_scope,
    has_interviews,
    has_offers,
    interview_participants,
    interviews,
    my_demand_ids,
    now,
    offer_versions,
    offers,
    principal_of,
    tenant_tz,
    users,
)

# Epic 65: kinds readable with any one of several permissions (agent Proposals: proposal:decide or ai:govern).
ALT_READ_PERMISSIONS: dict[str, tuple[str, ...]] = {}


def can_read_kind(principal: Principal, kind: str) -> bool:
    return any(principal.has(p) for p in (READ_PERMISSION[kind], *ALT_READ_PERMISSIONS.get(kind, ())))


def check_kind(principal: Principal, kind: str) -> None:
    if not can_read_kind(principal, kind):
        check(principal, READ_PERMISSION[kind])


READ_PERMISSION: dict[str, str] = {
    "candidates": "candidate:read",
    "demands": "demand:read",
    "applications": "application:read",
    "submissions": "submission:read",
    "interviews": "interview:read",
    "offers": "offer:read",
}
TERMINAL_STAGE_KEYS = ("hired", "rejected", "withdrawn")
INTERVIEW_ACTIVE_STATUSES = ("scheduled", "completed", "no_show")


@dataclasses.dataclass(frozen=True, slots=True)
class Column:
    key: str
    label: str
    type: str  # string | int | decimal | datetime | date | enum | money | bool | uuid
    expr: Any
    currency: Any = None  # money columns: the currency expression (exported as a second column)
    compensation: bool = False
    pii: bool = False
    labels: Mapping[str, str] | None = None
    sortable: bool = True
    exportable: bool = True
    finance: bool = False  # Epic 34: amount column of a finance list (export needs report:finance_export)

    def out(self, masked: bool = False) -> ColumnOut:
        return ColumnOut(
            key=self.key,
            label=self.label,
            type=self.type,
            compensation=self.compensation,
            pii=self.pii,
            masked=masked,
            sortable=self.sortable,
        )

    def enum_label(self, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        if self.labels and value in self.labels:
            return self.labels[value]
        return value.replace("_", " ").capitalize()


# ================================================================ lookups (correlated scalar subqueries)
def _user_name(col: Any) -> Any:
    return select(users.c.name).where(users.c.id == col).scalar_subquery()


def _candidate_name(col: Any) -> Any:
    return select(Candidate.full_name).where(Candidate.id == col).scalar_subquery()


def _demand(attr: Any, col: Any) -> Any:
    return select(attr).where(Demand.id == col).scalar_subquery()


def _client_name(col: Any) -> Any:
    return select(Client.name).where(Client.id == col).scalar_subquery()


def _stage(attr: Any, col: Any) -> Any:
    return select(attr).where(PipelineStage.id == col).scalar_subquery()


def _offer_version(attr: Any) -> Any:
    version_id = func.coalesce(offers.c.released_version_id, offers.c.current_version_id)
    return select(attr).where(offer_versions.c.id == version_id).scalar_subquery()


A, S, C, D = Application, Submission, Candidate, Demand
IV, OF = interviews.c, offers.c

COLUMNS: dict[str, tuple[Column, ...]] = {
    "candidates": (
        Column("human_id", "Candidate ID", "string", C.human_id),
        Column("full_name", "Name", "string", C.full_name, pii=True),
        Column(
            "email",
            "Email",
            "string",
            select(CandidateEmail.email)
            .where(CandidateEmail.candidate_id == C.id)
            .order_by(CandidateEmail.is_primary.desc(), CandidateEmail.created_at)
            .limit(1)
            .scalar_subquery(),
            pii=True,
            sortable=False,
        ),
        Column(
            "phone",
            "Phone",
            "string",
            select(CandidatePhone.phone_e164)
            .where(CandidatePhone.candidate_id == C.id)
            .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
            .limit(1)
            .scalar_subquery(),
            pii=True,
            sortable=False,
        ),
        Column("current_title", "Current title", "string", C.current_title),
        Column("current_employer", "Current employer", "string", C.current_employer),
        Column("city", "City", "string", C.location["city"].astext),
        Column("country", "Country", "string", C.location["country"].astext),
        Column("total_experience_months", "Experience (months)", "int", C.total_experience_months),
        Column("notice_period_days", "Notice period (days)", "int", C.notice_period_days),
        Column("source", "Source", "enum", C.source),
        Column("owner", "Owner", "string", _user_name(C.owner_id)),
        Column(
            "current_ctc",
            "Current CTC",
            "money",
            C.current_ctc_amount,
            C.current_ctc_currency,
            compensation=True,
        ),
        Column(
            "expected_ctc",
            "Expected CTC",
            "money",
            C.expected_ctc_amount,
            C.expected_ctc_currency,
            compensation=True,
        ),
        Column("created_at", "Created", "datetime", C.created_at),
    ),
    "demands": (
        Column("req_id", "REQ", "string", D.req_id),
        Column("title", "Title", "string", D.title),
        Column("client", "Client", "string", _client_name(D.client_id)),
        Column("status", "Status", "enum", D.status),
        Column("priority", "Priority", "enum", D.priority),
        Column("positions", "Positions", "int", D.positions),
        Column("positions_filled", "Positions filled", "int", D.positions_filled),
        Column("employment_type", "Employment type", "enum", D.employment_type),
        Column("location", "Location", "string", D.location),
        Column("work_mode", "Work mode", "enum", D.work_mode),
        Column("owner", "Owner", "string", _user_name(D.owner_id)),
        Column(
            "business_unit",
            "Business unit",
            "string",
            select(business_units.c.name).where(business_units.c.id == D.business_unit_id).scalar_subquery(),
        ),
        Column("bill_rate", "Bill rate", "money", D.bill_rate, D.currency, compensation=True),
        Column("pay_rate_min", "Pay rate (min)", "money", D.pay_rate_min, D.currency, compensation=True),
        Column("pay_rate_max", "Pay rate (max)", "money", D.pay_rate_max, D.currency, compensation=True),
        Column("rate_unit", "Rate unit", "enum", D.rate_unit),
        Column("opened_at", "Opened", "datetime", D.opened_at),
        Column("target_closure_date", "Target closure", "date", D.target_closure_date),
        Column("created_at", "Created", "datetime", D.created_at),
    ),
    "applications": (
        Column("human_id", "Application ID", "string", A.human_id),
        Column("candidate", "Candidate", "string", _candidate_name(A.candidate_id), pii=True),
        Column("demand_req_id", "REQ", "string", _demand(D.req_id, A.demand_id)),
        Column("demand_title", "Demand", "string", _demand(D.title, A.demand_id)),
        Column(
            "client",
            "Client",
            "string",
            select(Client.name)
            .join(Demand, Demand.client_id == Client.id)
            .where(Demand.id == A.demand_id)
            .scalar_subquery(),
        ),
        Column("stage", "Stage", "string", _stage(PipelineStage.label, A.stage_id)),
        Column("stage_key", "Stage key", "string", _stage(PipelineStage.key, A.stage_id), exportable=False),
        Column("source", "Source", "enum", A.source),
        Column("owner", "Owner", "string", _user_name(A.owner_id)),
        Column("last_client_feedback", "Client feedback", "enum", A.last_client_feedback),
        Column("stage_entered_at", "In stage since", "datetime", A.stage_entered_at),
        Column("created_at", "Added", "datetime", A.created_at),
        Column("candidate_id", "Candidate id", "uuid", A.candidate_id, sortable=False, exportable=False),
        Column("demand_id", "Demand id", "uuid", A.demand_id, sortable=False, exportable=False),
    ),
    "submissions": (
        Column("human_id", "Submission ID", "string", S.human_id),
        Column("candidate", "Candidate", "string", _candidate_name(S.candidate_id), pii=True),
        Column("demand_req_id", "REQ", "string", _demand(D.req_id, S.demand_id)),
        Column("demand_title", "Demand", "string", _demand(D.title, S.demand_id)),
        Column("client", "Client", "string", _client_name(S.client_id)),
        Column("status", "Status", "enum", S.status),
        Column("sent_at", "Sent", "datetime", S.sent_at),
        Column("sent_by", "Sent by", "string", _user_name(S.sent_by)),
        Column("feedback_status", "Client feedback", "enum", S.feedback_status),
        Column("pay_rate", "Pay rate", "money", S.pay_rate, S.currency, compensation=True),
        Column("bill_rate", "Bill rate", "money", S.bill_rate, S.currency, compensation=True),
        Column("margin_amount", "Margin", "money", S.margin_amount, S.currency, compensation=True),
        Column("margin_pct", "Margin %", "decimal", S.margin_pct, compensation=True),
        Column("rate_unit", "Rate unit", "enum", S.rate_unit),
        Column("created_at", "Created", "datetime", S.created_at),
        Column(
            "application_id", "Application id", "uuid", S.application_id, sortable=False, exportable=False
        ),
    ),
    "interviews": (
        Column("human_id", "Interview ID", "string", IV.human_id),
        Column("candidate", "Candidate", "string", _candidate_name(IV.candidate_id), pii=True),
        Column("demand_req_id", "REQ", "string", _demand(D.req_id, IV.demand_id)),
        Column("demand_title", "Demand", "string", _demand(D.title, IV.demand_id)),
        Column("stage_name", "Interview stage", "string", IV.stage_name),
        Column("round", "Round", "int", IV.round),
        Column("starts_at", "Starts", "datetime", IV.starts_at),
        Column("mode", "Mode", "enum", IV.mode, labels={"in_person": "In person"}),
        Column("status", "Status", "enum", IV.status, labels={"no_show": "No show"}),
        Column("organizer", "Organizer", "string", _user_name(IV.organizer_user_id)),
        Column(
            "application_id", "Application id", "uuid", IV.application_id, sortable=False, exportable=False
        ),
    ),
    "offers": (
        Column("human_id", "Offer ID", "string", OF.human_id),
        Column("candidate", "Candidate", "string", _candidate_name(OF.candidate_id), pii=True),
        Column("demand_req_id", "REQ", "string", _demand(D.req_id, OF.demand_id)),
        Column("client", "Client", "string", _client_name(OF.client_id)),
        Column("status", "Status", "enum", OF.status),
        Column("title", "Offer title", "string", _offer_version(offer_versions.c.title)),
        Column(
            "compensation",
            "Compensation",
            "money",
            _offer_version(offer_versions.c.pay_rate),
            _offer_version(offer_versions.c.pay_currency),
            compensation=True,
        ),
        Column(
            "bill_rate",
            "Bill rate",
            "money",
            _offer_version(offer_versions.c.bill_rate),
            _offer_version(offer_versions.c.bill_currency),
            compensation=True,
        ),
        Column(
            "margin_amount",
            "Margin",
            "money",
            _offer_version(offer_versions.c.margin_amount),
            _offer_version(offer_versions.c.bill_currency),
            compensation=True,
        ),
        Column(
            "margin_pct",
            "Margin %",
            "decimal",
            _offer_version(offer_versions.c.margin_pct),
            compensation=True,
        ),
        Column("released_at", "Released", "datetime", OF.released_at),
        Column("responded_at", "Responded", "datetime", OF.responded_at),
        Column("expires_at", "Expires", "datetime", OF.expires_at),
        Column("owner", "Owner", "string", _user_name(OF.owner_id)),
        Column(
            "application_id", "Application id", "uuid", OF.application_id, sortable=False, exportable=False
        ),
    ),
}
COLUMN_INDEX: dict[str, dict[str, Column]] = {
    kind: {c.key: c for c in cols} for kind, cols in COLUMNS.items()
}
DEFAULT_SORT: dict[str, str] = {
    "applications": "-created_at",
    "submissions": "-sent_at",
    "interviews": "starts_at",
    "offers": "-released_at",
}
ENTITY_ID: dict[str, Any] = {
    "candidates": C.id,
    "demands": D.id,
    "applications": A.id,
    "submissions": S.id,
    "interviews": IV.id,
    "offers": OF.id,
}


@dataclasses.dataclass(frozen=True, slots=True)
class ListQuery:
    kind: str
    base: Select[Any]  # scoped, filtered, ordered: one row (the entity id) per listed entity
    rows: Select[Any]  # id + the selected columns, same order
    columns: tuple[Column, ...]  # selected (exports: after removing masked compensation columns)
    masked_columns: tuple[str, ...]
    tz: ZoneInfo

    def count(self) -> Select[Any]:
        return select(func.count()).select_from(self.base.order_by(None).subquery())


# ================================================================ helpers
def _field_error(message: str, code: str = "validation_failed", **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def _me(value: str | None, principal: Principal) -> uuid.UUID | None:
    if value is None:
        return None
    return principal.id if value == "me" else uuid.UUID(value)


def _range(f: Any, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    start = end = None
    if f.from_ is not None:
        start = day_range(f.from_, f.from_, tz)[0]
    if f.to is not None:
        end = day_range(f.to, f.to, tz)[1]
    if start is not None and end is not None and start >= end:
        raise _field_error("`from` must not be after `to`", "invalid_range")
    return start, end


def _between(col: Any, start: datetime | None, end: datetime | None) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if start is not None:
        conds.append(col >= start)
    if end is not None:
        conds.append(col < end)
    return conds


def demand_condition(
    demand_col: Any,
    principal: Principal,
    permission: str,
    business_unit_ids: Sequence[uuid.UUID] | None = None,
    client_owner: uuid.UUID | None = None,
) -> ColumnElement[bool] | None:
    """Rows whose demand is visible under ``permission`` (plus optional BU / owned-client narrowing)."""
    cond = demand_scope(principal, permission)
    extra: list[ColumnElement[bool]] = []
    if business_unit_ids:
        extra.append(Demand.business_unit_id.in_(list(business_unit_ids)))
    if client_owner is not None:
        extra.append(Demand.client_id.in_(select(Client.id).where(Client.owner_id == client_owner)))
    if cond is None and not extra:
        return None
    where = [c for c in (cond, *extra) if c is not None]
    return demand_col.in_(select(Demand.id).where(*where))  # type: ignore[no-any-return]


def stage_ids(*keys: str) -> Select[Any]:
    return select(PipelineStage.id).where(PipelineStage.key.in_(keys))


def reached_condition(key: str) -> ColumnElement[bool]:
    """Match applications that are, or ever were (stage history), in the stage ``key``."""
    ids = stage_ids(key)
    return or_(
        A.stage_id.in_(ids),
        exists().where(
            ApplicationStageHistory.application_id == A.id, ApplicationStageHistory.to_stage_id.in_(ids)
        ),
    )


def has_interview_condition(application_col: Any) -> ColumnElement[bool]:
    return exists().where(
        interviews.c.application_id == application_col, interviews.c.status.in_(INTERVIEW_ACTIVE_STATUSES)
    )


def db_sourced_condition() -> ColumnElement[bool]:
    """SM-2: the submitted candidate existed before the demand opened."""
    return exists().where(
        Candidate.id == S.candidate_id,
        Demand.id == S.demand_id,
        Demand.opened_at.is_not(None),
        Candidate.created_at < Demand.opened_at,
    )


def mine_interview_condition(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Interviews the user takes part in, or on applications the user owns."""
    return or_(
        exists().where(
            interview_participants.c.interview_id == interviews.c.id,
            interview_participants.c.user_id == user_id,
            interview_participants.c.removed_at.is_(None),
        ),
        interviews.c.application_id.in_(select(A.id).where(A.owner_id == user_id)),
    )


def _order(stmt: Select[Any], kind: str, sort: str | None, entity_id: Any) -> Select[Any]:
    sort = sort or DEFAULT_SORT[kind]
    key = sort.lstrip("-")
    col = COLUMN_INDEX[kind].get(key)
    if col is None or not col.sortable:
        raise _field_error("Unknown sort column", sort=sort)
    expr = col.expr
    order = expr.desc().nulls_last() if sort.startswith("-") else expr.asc().nulls_last()
    return stmt.order_by(order, entity_id)


# ================================================================ per-kind base queries
async def _applications(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument] - common list-query signature
    principal: Principal,
    f: ApplicationListFilters,
    tz: ZoneInfo,
) -> Select[Any]:
    stmt = select(A.id)
    scope = demand_condition(
        A.demand_id,
        principal,
        READ_PERMISSION["applications"],
        f.business_unit_ids,
        _me(f.client_owner, principal),
    )
    if scope is not None:
        stmt = stmt.where(scope)
    if not f.include_archived:
        stmt = stmt.where(A.archived_at.is_(None))
    if f.stage:
        stmt = stmt.where(A.stage_id.in_(stage_ids(*split_csv(f.stage))))
    if f.active:
        stmt = stmt.where(A.stage_id.not_in(stage_ids(*TERMINAL_STAGE_KEYS)))
    if f.reached:
        stmt = stmt.where(reached_condition(f.reached))
    if f.source:
        stmt = stmt.where(A.source == f.source)
    if f.demand_id:
        stmt = stmt.where(A.demand_id == f.demand_id)
    if f.client_id:
        stmt = stmt.where(A.demand_id.in_(select(Demand.id).where(Demand.client_id == f.client_id)))
    owner = _me(f.owner, principal)
    if owner is not None:
        stmt = stmt.where(A.owner_id == owner)
    if f.scope == "mine":
        stmt = stmt.where(or_(A.owner_id == principal.id, A.demand_id.in_(my_demand_ids(principal))))
    start, end = _range(f, tz)
    stmt = stmt.where(*_between(A.created_at, start, end))
    return _order(stmt, "applications", f.sort, A.id)


async def _submissions(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument] - common list-query signature
    principal: Principal,
    f: SubmissionListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    stmt = select(S.id)
    scope = demand_condition(
        S.demand_id,
        principal,
        READ_PERMISSION["submissions"],
        f.business_unit_ids,
        _me(f.client_owner, principal),
    )
    if scope is not None:
        stmt = stmt.where(scope)
    sent_by = _me(f.sent_by, principal)
    if sent_by is not None:
        stmt = stmt.where(S.sent_by == sent_by)
    statuses = split_csv(f.status)
    if statuses:
        stmt = stmt.where(S.status.in_(statuses))
    if f.feedback:
        stmt = stmt.where(S.feedback_status == f.feedback)
    if f.older_than is not None:
        cutoff = business_days_ago(now(), f.older_than, tz)
        stmt = stmt.where(S.status == "sent", S.sent_at < cutoff)
    if f.db_sourced is not None:
        cond = db_sourced_condition()
        stmt = stmt.where(cond if f.db_sourced else ~cond)
    if f.has_interview is not None:
        if has_interviews(avail):
            cond = has_interview_condition(S.application_id)
            stmt = stmt.where(cond if f.has_interview else ~cond)
        elif f.has_interview:
            stmt = stmt.where(false())
    if f.demand_id:
        stmt = stmt.where(S.demand_id == f.demand_id)
    if f.client_id:
        stmt = stmt.where(S.client_id == f.client_id)
    start, end = _range(f, tz)
    stmt = stmt.where(*_between(S.sent_at, start, end))
    return _order(stmt, "submissions", f.sort, S.id)


async def _interviews(principal: Principal, f: InterviewListFilters, tz: ZoneInfo) -> Select[Any]:
    stmt = select(IV.id)
    permission = READ_PERMISSION["interviews"]
    scope = demand_condition(IV.demand_id, principal, permission, None, _me(f.client_owner, principal))
    if permission in principal.permissions:
        # Interviewers (``own`` scope) also see the interviews they take part in.
        participant = exists().where(
            interview_participants.c.interview_id == IV.id,
            interview_participants.c.user_id == principal.id,
            interview_participants.c.removed_at.is_(None),
        )
        scope = None if scope is None else or_(scope, participant)
    if scope is not None:
        stmt = stmt.where(scope)
    if f.business_unit_ids:
        stmt = stmt.where(
            IV.demand_id.in_(select(Demand.id).where(Demand.business_unit_id.in_(f.business_unit_ids)))
        )
    statuses = split_csv(f.status)
    stmt = stmt.where(IV.status.in_(statuses or INTERVIEW_ACTIVE_STATUSES))
    if f.mine:
        stmt = stmt.where(mine_interview_condition(principal.id))
    if f.demand_id:
        stmt = stmt.where(IV.demand_id == f.demand_id)
    if f.date:
        day = now().astimezone(tz).date() if f.date == "today" else date.fromisoformat(f.date)
        stmt = stmt.where(*_between(IV.starts_at, *day_range(day, day, tz)))
    start, end = _range(f, tz)
    stmt = stmt.where(*_between(IV.starts_at, start, end))
    return _order(stmt, "interviews", f.sort, IV.id)


async def _offers(principal: Principal, f: OfferListFilters, tz: ZoneInfo) -> Select[Any]:
    stmt = select(OF.id)
    scope = demand_condition(
        OF.demand_id,
        principal,
        READ_PERMISSION["offers"],
        f.business_unit_ids,
        _me(f.client_owner, principal),
    )
    if scope is not None:
        stmt = stmt.where(scope)
    statuses = split_csv(f.status)
    if statuses:
        stmt = stmt.where(OF.status.in_(statuses))
    if f.mine:
        stmt = stmt.where(OF.application_id.in_(select(A.id).where(A.owner_id == principal.id)))
    if f.demand_id:
        stmt = stmt.where(OF.demand_id == f.demand_id)
    if f.client_id:
        stmt = stmt.where(OF.client_id == f.client_id)
    start, end = _range(f, tz)
    stmt = stmt.where(*_between(OF.released_at, start, end))
    return _order(stmt, "offers", f.sort, OF.id)


FILTER_MODELS: dict[str, type[BaseModel]] = {
    "applications": ApplicationListFilters,
    "submissions": SubmissionListFilters,
    "interviews": InterviewListFilters,
    "offers": OfferListFilters,
}


def filter_model(kind: str) -> type[BaseModel]:
    if kind == "candidates":
        from app.schemas.candidates import CandidateFilters

        return CandidateFilters
    if kind == "demands":
        from app.schemas.demands import DemandFilters

        return DemandFilters
    return FILTER_MODELS[kind]


def parse_filters(kind: str, raw: Mapping[str, Any]) -> BaseModel:
    """Validate export filters with the kind's filter model; unknown keys -> 422."""
    model = filter_model(kind)
    known = set(model.model_fields) | {
        f.alias for f in model.model_fields.values() if f.alias is not None
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise _field_error("Unknown filter(s)", "unknown_filters", filters=unknown)
    from pydantic import ValidationError

    try:
        return model.model_validate(dict(raw))
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_url=False)]
        raise _field_error("Invalid filters", "invalid_filters", errors=errors) from exc


def _select_columns(kind: str, requested: Sequence[str] | None, *, export: bool) -> tuple[Column, ...]:
    index = COLUMN_INDEX[kind]
    if requested:
        unknown = [k for k in requested if k not in index or (export and not index[k].exportable)]
        if unknown:
            raise _field_error("Unknown column(s)", "unknown_columns", columns=unknown)
        return tuple(index[k] for k in dict.fromkeys(requested))
    return tuple(c for c in COLUMNS[kind] if c.exportable or not export)


async def _ensure_referenced(db: AsyncSession, principal: Principal, kind: str, filters: BaseModel) -> None:
    """Require ``demand_id`` / ``client_id`` filters to name records the caller can see (else 404, which also
    covers another tenant's ids: RLS hides them)."""
    demand_id = getattr(filters, "demand_id", None)
    if demand_id is not None:
        cond = demand_scope(principal, READ_PERMISSION[kind])
        stmt = select(Demand.id).where(Demand.id == demand_id)
        if cond is not None:
            stmt = stmt.where(cond)
        if (await db.execute(stmt)).first() is None:
            raise NotFound("Demand not found")
    client_id = getattr(filters, "client_id", None)
    if client_id is not None and kind not in ("candidates", "demands"):
        if (await db.execute(select(Client.id).where(Client.id == client_id))).first() is None:
            raise NotFound("Client not found")


async def build(
    ctx: RequestContext,
    db: AsyncSession,
    kind: str,
    filters: BaseModel,
    *,
    columns: Sequence[str] | None = None,
    export: bool = False,
) -> ListQuery:
    principal = principal_of(ctx)
    check_kind(principal, kind)
    tz = await tenant_tz(db)
    avail = await available(db)
    if kind not in ("candidates", "demands"):
        await _ensure_referenced(db, principal, kind, filters)
    if kind == "candidates":
        from app.services import candidates as candidates_service

        entity = await _entity_query(candidates_service.list_query(ctx, filters), C.id)  # type: ignore[arg-type]
    elif kind == "demands":
        from app.services import demands as demands_service

        entity = await _entity_query(await demands_service.list_query(ctx, db, filters), D.id)  # type: ignore[arg-type]
    elif kind == "applications":
        entity = await _applications(ctx, principal, filters, tz)  # type: ignore[arg-type]
    elif kind == "submissions":
        entity = await _submissions(ctx, principal, filters, tz, avail)  # type: ignore[arg-type]
    elif kind == "interviews":
        if not has_interviews(avail):
            raise NotFound("Interviews are not available", code="not_available")
        entity = await _interviews(principal, filters, tz)  # type: ignore[arg-type]
    elif kind == "offers":
        if not has_offers(avail):
            raise NotFound("Offers are not available", code="not_available")
        entity = await _offers(principal, filters, tz)  # type: ignore[arg-type]
    elif kind in P2_BUILDERS:
        entity = await P2_BUILDERS[kind](ctx, db, principal, filters, tz, avail)
    else:  # pragma: no cover - guarded by the schema
        raise NotFound("Unknown list")
    selected = _select_columns(kind, columns, export=export)
    masked: tuple[str, ...] = ()
    if not can_view_compensation(principal):
        masked = tuple(c.key for c in selected if c.compensation)
        if export:
            selected = tuple(c for c in selected if not c.compensation)
    if export and kind in FINANCE_EXPORT_KINDS and not principal.has("report:finance_export"):
        # Epic 34 (A4): finance amounts are exported only with report:finance_export.
        masked = masked + tuple(c.key for c in selected if c.finance and c.key not in masked)
        selected = tuple(c for c in selected if not c.finance)
    labels: list[Any] = [ENTITY_ID[kind].label("id")]
    for col in selected:
        if col.compensation and col.key in masked:
            continue  # never selected for a principal without compensation:view
        labels.append(col.expr.label(col.key))
        if col.type == "money":
            labels.append(col.currency.label(f"{col.key}__currency"))
    return ListQuery(
        kind=kind,
        base=entity,
        rows=entity.with_only_columns(*labels),
        columns=selected,
        masked_columns=masked,
        tz=tz,
    )


# ================================================================ Epic 34: list-kind registry (A4)
P2Builder = Any  # async (ctx, db, principal, filters, tz, avail) -> Select of the entity id
P2_BUILDERS: dict[str, P2Builder] = {}
FINANCE_EXPORT_KINDS: set[str] = set()


@dataclasses.dataclass(frozen=True, slots=True)
class ListKindDef:
    kind: str
    filter_model: type[BaseModel]
    columns: tuple[Column, ...]
    query: P2Builder
    read_permission: str
    entity_id: Any
    default_sort: str
    finance_export: bool = False


def register_list_kind(defn: ListKindDef) -> None:
    """Register a list kind (drill-down + export source) next to the P1 kinds."""
    COLUMNS[defn.kind] = defn.columns
    COLUMN_INDEX[defn.kind] = {c.key: c for c in defn.columns}
    DEFAULT_SORT[defn.kind] = defn.default_sort
    ENTITY_ID[defn.kind] = defn.entity_id
    READ_PERMISSION[defn.kind] = defn.read_permission
    FILTER_MODELS[defn.kind] = defn.filter_model
    P2_BUILDERS[defn.kind] = defn.query
    if defn.finance_export:
        FINANCE_EXPORT_KINDS.add(defn.kind)


async def _entity_query(stmt: Select[Any], id_col: Any) -> Select[Any]:
    return stmt.with_only_columns(id_col)


# ================================================================ list endpoint
def _json_value(col: Column, row: Mapping[str, Any]) -> Any:
    value = row.get(col.key)
    if col.type == "money":
        if value is None:
            return None
        return {"amount": f"{Decimal(value):.2f}", "currency": row.get(f"{col.key}__currency")}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def list_rows(
    ctx: RequestContext, db: AsyncSession, kind: str, filters: BaseModel, params: PageParams
) -> dict[str, Any]:
    query = await build(ctx, db, kind, filters)
    total = int((await db.execute(query.count())).scalar_one())
    result = await db.execute(query.rows.limit(params.page_size).offset(params.offset))
    items: list[dict[str, Any]] = []
    for row in result.mappings():
        item: dict[str, Any] = {"id": str(row["id"])}
        for col in query.columns:
            item[col.key] = None if col.key in query.masked_columns else _json_value(col, row)  # type: ignore[arg-type]
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
        "columns": [c.out(masked=c.key in query.masked_columns) for c in query.columns],
    }


__all__ = [
    "COLUMNS",
    "READ_PERMISSION",
    "Column",
    "ListKindDef",
    "ListQuery",
    "build",
    "list_rows",
    "parse_filters",
    "register_list_kind",
]
