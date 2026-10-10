"""Reporting schemas (Epic 11): dashboards / tiles, report list filters and responses, exports."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

TileKind = Literal["kpi", "table", "series", "breakdown", "funnel"]
TileSize = Literal["s", "m", "l"]
TileStatus = Literal["ok", "timeout", "unavailable"]
ListKind = Literal["applications", "submissions", "interviews", "offers"]
P2ListKind = Literal[
    "leads",
    "opportunities",
    "vendors",
    "vendor_submissions",
    "placements",
    "timesheets",
    "expense_claims",
    "invoices",
    "payments",
    "bgv_cases",
    # Epic 52 (A5): Phase 3 list kinds (``reporting.lists_p3``; foundation 0.3 ``ck_exports_kind``).
    "talent_pools",
    "campaigns",
    "assessments",
    "campus_drives",
    "recruitment_events",
    "projects",
    "immigration_cases",
    "report_definitions",
    "referrals",
    "workforce_plans",
    # Epic 65 (A8): Phase 4 list kinds (``reporting.lists_p4``; foundation 0.3 ``ck_exports_kind``).
    "agent_runs",
    "agent_proposals",
]
ExportKind = Literal[
    "candidates",
    "demands",
    "applications",
    "submissions",
    "interviews",
    "offers",
    # Epic 34 (A4): Phase 2 list kinds.
    "leads",
    "opportunities",
    "vendors",
    "vendor_submissions",
    "placements",
    "timesheets",
    "expense_claims",
    "invoices",
    "payments",
    "bgv_cases",
    # Epic 52 (A5): Phase 3 list kinds.
    "talent_pools",
    "campaigns",
    "assessments",
    "campus_drives",
    "recruitment_events",
    "projects",
    "immigration_cases",
    "report_definitions",
    "referrals",
    "workforce_plans",
    # Epic 65 (A8): Phase 4 list kinds (``reporting.lists_p4``; foundation 0.3 ``ck_exports_kind``).
    "agent_runs",
    "agent_proposals",
]
ExportFormat = Literal["csv", "xlsx"]
ExportStatus = Literal["queued", "running", "done", "failed", "expired"]

SortParam = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^-?[a-z_]{1,40}$")]
MeOrId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
CsvParam = Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)]


def split_csv(value: str | None) -> list[str]:
    """``"released,viewed"`` -> ``["released", "viewed"]`` (blank items dropped)."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _me_or_uuid(value: str | None) -> str | None:
    if value is None or value == "me":
        return value
    uuid.UUID(value)
    return value


# ================================================================ dashboards
class LinkOut(BaseModel):
    """A drill-down target: a frontend path plus its query parameters (lists = repeated parameters)."""

    path: str
    query: dict[str, str | list[str]] = Field(default_factory=dict)


class MoneyOut(BaseModel):
    """AD-12 money: an exact decimal string and its currency (never a float)."""

    amount: str
    currency: str


class TileDefOut(BaseModel):
    key: str
    title: str
    kind: TileKind
    size: TileSize
    description: str
    available: bool = True
    compensation: bool = False  # Epic 34
    estimate: bool = False  # Epic 34


class DashboardOut(BaseModel):
    key: Literal[
        "recruiter",
        "manager",
        "account",
        "finance",
        "account_ops",
        "onboarding",
        "compliance",
        "delivery",
        "executive",
        # Epic 52: Phase 3 dashboards.
        "marketing",
        "referrals",
        "campus",
        "mobility",
        "skills",
        "projects",
        "immigration",
        "predictions",
        # Epic 65: Phase 4 dashboards.
        "agents",
        "agent_outcomes",
    ]
    title: str
    default: bool = False
    tiles: list[TileDefOut]


class DashboardListOut(BaseModel):
    items: list[DashboardOut]


class TileOut(BaseModel):
    key: str
    kind: TileKind
    title: str
    status: TileStatus = "ok"
    value: float | int | None = None
    unit: str | None = None
    n: int | None = None
    rows: list[dict[str, Any]] | None = None
    series: list[dict[str, Any]] | None = None
    link: LinkOut | None = None
    row_links: list[LinkOut | None] | None = None
    computed_at: datetime
    granularity: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    # Epic 34 (Phase 2 tiles): AD-12 money, WIP estimate flag, summary/live source, masking, as-of day.
    money: MoneyOut | None = None
    estimate: bool | None = None
    source: Literal["summary", "live"] | None = None
    masked: bool | None = None
    as_of: date | None = None
    series_keys: list[dict[str, Any]] | None = None
    badge: str | None = None  # Epic 52: "Supporting signal" on prediction tiles (NFR-37)


class ExecCellOut(BaseModel):
    month: date
    value: Any = None  # MoneyOut dict | decimal string | int | None
    source: Literal["summary", "live"] = "summary"


class ExecRowOut(BaseModel):
    key: str
    label: str
    kind: Literal["money", "percent", "count", "number"]
    masked: bool = False
    cells: list[ExecCellOut]


class ExecGridOut(BaseModel):
    legal_entity_id: uuid.UUID
    months: list[date]
    rows: list[ExecRowOut]
    computed_at: datetime


class ReconciliationIssueOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    month: date
    figure: str
    summary: str | None
    live: str | None
    detected_at: datetime
    alerted: bool


class ReconciliationOut(BaseModel):
    items: list[ReconciliationIssueOut]


# ================================================================ report lists
class _Filters(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    business_unit_ids: list[uuid.UUID] | None = None
    client_owner: Literal["me"] | None = None
    sort: SortParam | None = None


class ApplicationListFilters(_Filters):
    stage: CsvParam | None = Field(default=None, description="Stage key(s), comma separated")
    reached: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    source: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    demand_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    owner: MeOrId | None = Field(default=None, description="`me` or a user id")
    scope: Literal["mine"] | None = None
    active: bool | None = Field(default=None, description="true: only non-terminal stages")
    include_archived: bool = False

    @field_validator("owner")
    @classmethod
    def _owner(cls, value: str | None) -> str | None:
        return _me_or_uuid(value)


class SubmissionListFilters(_Filters):
    sent_by: MeOrId | None = Field(default=None, description="`me` or a user id")
    status: CsvParam | None = None
    feedback: Literal["none", "interested", "rejected_pending", "rejected", "on_hold"] | None = None
    older_than: Annotated[int, Field(ge=0, le=365)] | None = Field(
        default=None, description="Sent more than N business days ago (and still `sent`)"
    )
    db_sourced: bool | None = None
    has_interview: bool | None = None
    demand_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None

    @field_validator("sent_by")
    @classmethod
    def _sent_by(cls, value: str | None) -> str | None:
        return _me_or_uuid(value)


class InterviewListFilters(_Filters):
    date: Annotated[str, StringConstraints(pattern=r"^(today|\d{4}-\d{2}-\d{2})$")] | None = None
    mine: bool | None = None
    status: CsvParam | None = Field(default=None, description="Default: every status except cancelled")
    demand_id: uuid.UUID | None = None


class OfferListFilters(_Filters):
    status: CsvParam | None = None
    mine: bool | None = Field(default=None, description="Offers on applications I own")
    demand_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None


class ColumnOut(BaseModel):
    key: str
    label: str
    type: str
    compensation: bool = False
    pii: bool = False
    masked: bool = False
    sortable: bool = True


class ReportListOut(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    columns: list[ColumnOut]


# ================================================================ exports
class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExportKind
    format: ExportFormat
    filters: dict[str, Any] = Field(default_factory=dict)
    columns: list[Annotated[str, StringConstraints(max_length=60)]] | None = Field(
        default=None, max_length=100
    )


class ExportOut(BaseModel):
    id: uuid.UUID
    kind: ExportKind
    format: ExportFormat
    mode: Literal["sync", "async"]
    status: ExportStatus
    filters: dict[str, Any]
    columns: list[str]
    masked_columns: list[str]
    row_count: int | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    downloaded_at: datetime | None
    download_url: str | None = None


class ExportAccepted(BaseModel):
    export_id: uuid.UUID
    status: ExportStatus
    row_count: int
    masked_columns: list[str]
