"""Sales CRM API models (Epic 15, FR-83..FR-87). Money is a decimal **string** (AD-12)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.clients import _blank_to_none, normalize_gstin, normalize_phone, normalize_website

LeadStatus = Literal["new", "contacted", "qualified", "converted", "disqualified"]
OpportunityStage = Literal["qualification", "proposal", "negotiation", "won", "lost"]
Service = Literal["contract_staffing", "permanent_placement", "project", "other"]
ReasonKind = Literal["lead_disqualify", "opportunity_lost", "reopen"]
RecordKind = Literal["client", "contract", "rate_card", "demand"]
RateUnit = Literal["hour", "day", "month", "year"]
ContractType = Literal["msa", "sow"]

Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
OptText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{3}$")]
Code = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9_]{1,40}$")]
MoneyIn = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2)]
RateIn = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]
Years = Annotated[Decimal, Field(ge=0, le=99, max_digits=4, decimal_places=1)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================ settings / codes / sources
class CrmSettingsOut(BaseModel):
    stage_probabilities: dict[str, int]
    default_currency: str


class CrmSettingsUpdate(_In):
    stage_probabilities: dict[OpportunityStage, Annotated[int, Field(ge=0, le=100)]] | None = None
    default_currency: Currency | None = None


class ReasonCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ReasonKind
    code: str
    label: str
    archived_at: datetime | None


class ReasonCodeIn(_In):
    kind: ReasonKind
    code: Code
    label: Text200


class ReasonCodeUpdate(_In):
    label: Text200 | None = None
    archived: bool | None = None


class LeadSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    label: str
    archived_at: datetime | None


class LeadSourceIn(_In):
    code: Code
    label: Text200


class LeadSourceUpdate(_In):
    label: Text200 | None = None
    archived: bool | None = None


# ================================================================ leads
class EstimatedRole(BaseModel):
    title: Text200
    positions: Annotated[int, Field(ge=1, le=10000)] = 1


class _LeadFields(_In):
    website: Annotated[str, StringConstraints(max_length=500)] | None = None
    gstin: str | None = None
    industry: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    location: OptText | None = None
    contact_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    contact_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    contact_email: Annotated[EmailStr, StringConstraints(max_length=254)] | None = None
    contact_phone: Annotated[str, StringConstraints(max_length=40)] | None = None
    source_code: Code | None = None
    estimated_roles: Annotated[list[EstimatedRole], Field(max_length=50)] | None = None
    estimated_positions: Annotated[int, Field(ge=0, le=100000)] | None = None
    expected_annual_revenue: MoneyIn | None = None
    currency: Currency | None = None
    owner_id: uuid.UUID | None = None

    @field_validator(
        "website", "gstin", "industry", "location", "contact_name", "contact_title", "contact_email",
        "contact_phone", "source_code", mode="before",
    )  # fmt: skip
    @classmethod
    def _blank(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, value: str | None) -> str | None:
        return normalize_gstin(value)

    @field_validator("website")
    @classmethod
    def _website(cls, value: str | None) -> str | None:
        return normalize_website(value)

    @field_validator("contact_email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)


class LeadCreate(_LeadFields):
    company_name: Text200
    acknowledge_duplicates: bool = False


class LeadUpdate(_LeadFields):
    company_name: Text200 | None = None
    acknowledge_duplicates: bool = False


class DuplicateCheckIn(_In):
    company_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    website: Annotated[str, StringConstraints(max_length=500)] | None = None
    gstin: Annotated[str, StringConstraints(max_length=20)] | None = None
    exclude_lead_id: uuid.UUID | None = None


class DuplicateMatch(BaseModel):
    kind: Literal["client", "lead"]
    id: uuid.UUID
    name: str
    reason: Literal["name", "similar_name", "domain", "gstin"]
    human_id: str | None = None


class UserRef(BaseModel):
    id: uuid.UUID
    name: str


class LeadOut(BaseModel):
    id: uuid.UUID
    human_id: str
    company_name: str
    website: str | None
    website_domain: str | None
    gstin: str | None
    industry: str | None
    location: str | None
    contact_name: str | None
    contact_title: str | None
    contact_email: str | None
    contact_phone: str | None
    source_code: str | None
    estimated_roles: list[dict[str, Any]]
    estimated_positions: int | None
    expected_annual_revenue: str | None
    currency: str
    owner_id: uuid.UUID
    owner: UserRef | None
    status: LeadStatus
    disqualify_reason_code: str | None
    disqualify_note: str | None
    converted_at: datetime | None
    converted_opportunity_id: uuid.UUID | None
    converted_client_id: uuid.UUID | None
    converted_contact_id: uuid.UUID | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LeadPage(BaseModel):
    items: list[LeadOut]
    total: int
    page: int
    page_size: int


class LeadTransitionIn(_In):
    to: LeadStatus
    reason_code: Code | None = None
    note: Note | None = None


class StatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_value: str | None
    to_value: str
    actor_kind: str
    actor_id: uuid.UUID | None
    reason_code: str | None
    note: str | None
    competitor: str | None
    at: datetime


# ================================================================ import
class ImportErrorRow(BaseModel):
    row: int
    field: str | None
    code: str


class LeadImportOut(BaseModel):
    batch_id: uuid.UUID
    status: Literal["queued", "running", "completed", "failed"]
    row_count: int
    created: int = 0
    skipped_duplicates: int = 0
    errors: list[ImportErrorRow] = Field(default_factory=list)
    duplicate_of: uuid.UUID | None = None


# ================================================================ opportunities
class OpportunityLineIn(_In):
    title: Text300
    positions: Annotated[int, Field(ge=1, le=10000)] = 1
    expected_bill_rate: RateIn | None = None
    currency: Currency | None = None
    rate_unit: RateUnit | None = None
    location: OptText | None = None
    experience_min_years: Years | None = None
    experience_max_years: Years | None = None


class OpportunityLineOut(BaseModel):
    id: uuid.UUID
    title: str
    positions: int
    expected_bill_rate: str | None
    currency: str
    rate_unit: str | None
    location: str | None
    experience_min_years: str | None
    experience_max_years: str | None
    position: int


class OpportunityCreate(_In):
    name: Text300
    client_id: uuid.UUID | None = None  # required for a direct create; set by lead conversion
    primary_contact_id: uuid.UUID | None = None
    service: Service = "contract_staffing"
    estimated_revenue: MoneyIn = Decimal("0")
    currency: Currency | None = None
    expected_close_date: date | None = None
    owner_id: uuid.UUID | None = None
    account_manager_id: uuid.UUID | None = None
    probability_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)] | None = None
    lines: Annotated[list[OpportunityLineIn], Field(max_length=50)] = Field(default_factory=list)


class OpportunityUpdate(_In):
    name: Text300 | None = None
    primary_contact_id: uuid.UUID | None = None
    service: Service | None = None
    estimated_revenue: MoneyIn | None = None
    currency: Currency | None = None
    expected_close_date: date | None = None
    owner_id: uuid.UUID | None = None
    account_manager_id: uuid.UUID | None = None
    probability_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)] | None = None


class OpportunityLinesIn(_In):
    lines: Annotated[list[OpportunityLineIn], Field(max_length=50)]


class OpportunityTransitionIn(_In):
    to: OpportunityStage
    expected_stage: OpportunityStage
    reason_code: Code | None = None
    competitor: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    note: Note | None = None


class OpportunityOut(BaseModel):
    id: uuid.UUID
    human_id: str
    name: str
    client_id: uuid.UUID
    client_name: str | None
    primary_contact_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    service: Service
    estimated_revenue: str
    weighted_revenue: str
    currency: str
    probability_pct: str
    probability_overridden: bool
    expected_close_date: date | None
    owner_id: uuid.UUID
    owner: UserRef | None
    account_manager_id: uuid.UUID | None
    account_manager: UserRef | None
    stage: OpportunityStage
    stage_entered_at: datetime
    lost_reason_code: str | None
    lost_competitor: str | None
    lost_note: str | None
    won_at: datetime | None
    lost_at: datetime | None
    version: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    lines: list[OpportunityLineOut] = Field(default_factory=list)


class OpportunityPage(BaseModel):
    items: list[OpportunityOut]
    total: int
    page: int
    page_size: int


class CurrencyTotal(BaseModel):
    currency: str
    estimated: str
    weighted: str


class BoardColumn(BaseModel):
    stage: OpportunityStage
    count: int
    totals: list[CurrencyTotal]
    items: list[OpportunityOut]


class BoardOut(BaseModel):
    columns: list[BoardColumn]


class SummaryGroup(BaseModel):
    key: str
    label: str
    count: int
    totals: list[CurrencyTotal]


class SummaryOut(BaseModel):
    group_by: Literal["owner", "stage", "close_month"]
    groups: list[SummaryGroup]


class ClientOpportunitiesOut(BaseModel):
    open_count: int
    weighted_pipeline: list[CurrencyTotal]
    items: list[OpportunityOut]


# ================================================================ lead conversion
class ConvertClientLink(_In):
    mode: Literal["link"]
    client_id: uuid.UUID


class ConvertClientCreate(_In):
    mode: Literal["create"]
    name: Text200
    website: Annotated[str, StringConstraints(max_length=500)] | None = None
    industry: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    gstin: str | None = None
    owner_id: uuid.UUID | None = None

    @field_validator("website", "industry", "gstin", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, value: str | None) -> str | None:
        return normalize_gstin(value)

    @field_validator("website")
    @classmethod
    def _website(cls, value: str | None) -> str | None:
        return normalize_website(value)


class ConvertContactLink(_In):
    mode: Literal["link"]
    contact_id: uuid.UUID


class ConvertContactCreate(_In):
    mode: Literal["create"]
    first_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    last_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    email: Annotated[EmailStr, StringConstraints(max_length=254)] | None = None
    phone: Annotated[str, StringConstraints(max_length=40)] | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None

    @field_validator("email", "phone", "title", "last_name", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)


class ConvertContactNone(_In):
    mode: Literal["none"]


class LeadConvertIn(_In):
    client: Annotated[ConvertClientLink | ConvertClientCreate, Field(discriminator="mode")]
    contact: Annotated[
        ConvertContactLink | ConvertContactCreate | ConvertContactNone, Field(discriminator="mode")
    ] = ConvertContactNone(mode="none")
    opportunity: OpportunityCreate


class LeadConvertOut(BaseModel):
    lead: LeadOut
    opportunity: OpportunityOut
    client_id: uuid.UUID
    contact_id: uuid.UUID | None


# ================================================================ Won conversion
class WonPreviewClient(BaseModel):
    id: uuid.UUID
    name: str
    current_status: str
    will_activate: bool


class WonPreviewContract(BaseModel):
    type: ContractType
    title: str
    legal_entity_id: uuid.UUID | None
    start_date: date


class WonPreviewTemplate(BaseModel):
    id: uuid.UUID
    name: str


class LegalEntityRef(BaseModel):
    id: uuid.UUID
    name: str


class WonPreviewDemand(BaseModel):
    line_id: uuid.UUID
    title: str
    positions: int
    bill_rate: str | None
    currency: str
    rate_unit: str | None
    location: str | None
    owner_id: uuid.UUID | None


class WonPreviewOut(BaseModel):
    opportunity_id: uuid.UUID
    client: WonPreviewClient
    contract: WonPreviewContract
    legal_entities: list[LegalEntityRef]
    rate_card_templates: list[WonPreviewTemplate]
    demands: list[WonPreviewDemand]
    commercials_available: bool


class WinContractIn(_In):
    create: bool = True
    type: ContractType = "msa"
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    legal_entity_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    parent_contract_id: uuid.UUID | None = None
    file_ids: Annotated[list[uuid.UUID], Field(max_length=20)] = Field(default_factory=list)


class WinRateCardIn(_In):
    template_id: uuid.UUID
    effective_from: date


class WinDemandOverrides(_In):
    title: Text300 | None = None
    positions: Annotated[int, Field(ge=1, le=10000)] | None = None
    location: OptText | None = None
    bill_rate: RateIn | None = None


class WinDemandIn(_In):
    line_id: uuid.UUID
    create: bool = True
    overrides: WinDemandOverrides | None = None


class WinIn(_In):
    activate_client: bool = True
    contract: WinContractIn | None = None
    rate_card: WinRateCardIn | None = None
    demands: Annotated[list[WinDemandIn], Field(max_length=50)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_lines(self) -> "WinIn":
        ids = [d.line_id for d in self.demands]
        if len(ids) != len(set(ids)):
            raise ValueError("each line_id may appear once")
        return self


class CreatedRecord(BaseModel):
    kind: RecordKind
    id: uuid.UUID
    label: str | None = None
    line_id: uuid.UUID | None = None


class WinOut(BaseModel):
    opportunity: OpportunityOut
    client_id: uuid.UUID
    client_activated: bool
    contract_id: uuid.UUID | None
    rate_card_id: uuid.UUID | None
    demand_ids: list[uuid.UUID]
    records: list[CreatedRecord]


class RecordLinkOut(BaseModel):
    opportunity_id: uuid.UUID
    human_id: str
    name: str


# ================================================================ dashboard
class StageAmount(BaseModel):
    stage: str
    count: int
    totals: list[CurrencyTotal]


class MonthAmount(BaseModel):
    month: str  # YYYY-MM, or "overdue"
    count: int
    totals: list[CurrencyTotal]


class Tile(BaseModel):
    link: str


class PipelineTile(Tile):
    by_stage: list[StageAmount]
    by_close_month: list[MonthAmount]


class CountTotalsTile(Tile):
    count: int
    totals: list[CurrencyTotal]


class WinRateTile(Tile):
    won: int
    lost: int
    win_rate_pct: str | None
    avg_sales_cycle_days: str | None


class SourceRow(BaseModel):
    source_code: str
    created: int
    converted: int
    conversion_rate_pct: str | None


class LeadsBySourceTile(Tile):
    rows: list[SourceRow]


class CountTile(Tile):
    count: int


class DashboardOut(BaseModel):
    from_date: date
    to_date: date
    pipeline: PipelineTile
    closing_this_month: CountTotalsTile
    win_rate: WinRateTile
    leads_by_source: LeadsBySourceTile
    overdue_tasks: CountTile
