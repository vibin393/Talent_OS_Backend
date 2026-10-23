# ruff: file-ignore[line-too-long, unused-function-argument]
"""Phase 3 list kinds (Epic 52 A5, FR-262): drill-down lists and export sources for the ten kinds allowed by
``ck_exports_kind`` (foundation 0.3).

Registered into the P1 ``lists`` registry (``register_list_kind``), so ``GET /reports/lists/{kind}`` and the P1 export
engine (50k rows, 5k sync, formula-injection guard, money two-column format, audit ``export.run``) serve them
unchanged. Row scope is the owner's own list rule: the owner's ``scoped`` / ``visible`` helpers are reused (reporting
is the top-layer reader, AD-40), so a list total equals the owner's list endpoint under the same principal.

Masking: project budget / burn, reward amounts and plan budgets are ``compensation`` columns (removed from exports and
nulled in lists without ``compensation:view``). Immigration receipt numbers appear only as ``*_last4`` (AD-35). No
self-ID, proctoring, assessment answer or fairness column exists in any kind.
"""

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.context import RequestContext
from app.core.permissions import Principal
from app.models.assessments import Assessment
from app.models.campaigns import Campaign
from app.models.campus import CampusDrive, RecruitmentEvent
from app.models.candidates import Candidate
from app.models.clients import Client
from app.models.demands import Demand
from app.models.immigration import ImmigrationCase, VisaType
from app.models.projects import Project, ProjectBudget, ProjectBurn
from app.models.referrals import Referral, ReferralReward
from app.models.report_builder import ReportDefinition
from app.models.talent_pools import TalentPool
from app.models.workforce import WorkforcePlan, WorkforcePlanLine
from app.services.reporting import lists
from app.services.reporting.common import users
from app.services.reporting.lists import Column, ListKindDef, register_list_kind
from app.services.reporting.lists_p2 import P2ListFilters, _common


def _user_name(col: Any) -> Any:
    return select(users.c.name).where(users.c.id == col).scalar_subquery()


def _client_name(col: Any) -> Any:
    return select(Client.name).where(Client.id == col).scalar_subquery()


def _q(stmt: Select[Any], f: P2ListFilters, *cols: Any) -> Select[Any]:
    from sqlalchemy import or_

    term = (str(f.q) if f.q else "").strip()
    if not term:
        return stmt
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return stmt.where(or_(*(c.ilike(like, escape="\\") for c in cols)))


# ================================================================ talent pools (Epic 38)
async def _talent_pools(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.talent_pools import common

    stmt = select(TalentPool.id).where(common.visible(principal))
    stmt = stmt.where(TalentPool.status == "active") if not f.status else stmt
    stmt = _q(_common(stmt, f, status_col=TalentPool.status), f, TalentPool.name)
    return lists._order(stmt, "talent_pools", f.sort, TalentPool.id)


POOL_COLUMNS = (
    Column("name", "Pool", "string", TalentPool.name),
    Column("kind", "Kind", "enum", TalentPool.kind),
    Column("visibility", "Visibility", "enum", TalentPool.visibility),
    Column("status", "Status", "enum", TalentPool.status),
    Column("member_count", "Members", "int", TalentPool.member_count),
    Column("owner", "Owner", "string", _user_name(TalentPool.owner_id)),
    Column("last_refreshed_at", "Last refreshed", "datetime", TalentPool.last_refreshed_at),
    Column("created_at", "Created", "datetime", TalentPool.created_at),
)


# ================================================================ campaigns (Epic 39): tenant-wide within campaign:read
async def _campaigns(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    stmt = _q(_common(select(Campaign.id), f, status_col=Campaign.status), f, Campaign.name)
    return lists._order(stmt, "campaigns", f.sort, Campaign.id)


CAMPAIGN_COLUMNS = (
    Column("name", "Campaign", "string", Campaign.name),
    Column("channel", "Channel", "enum", Campaign.channel),
    Column("status", "Status", "enum", Campaign.status),
    Column("owner", "Owner", "string", _user_name(Campaign.owner_id)),
    Column("scheduled_at", "Scheduled", "datetime", Campaign.scheduled_at),
    Column("started_at", "Started", "datetime", Campaign.started_at),
    Column("completed_at", "Completed", "datetime", Campaign.completed_at),
    Column("created_at", "Created", "datetime", Campaign.created_at),
)


# ================================================================ assessments (Epic 40): tenant-wide within assessment:read
async def _assessments(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    stmt = _q(_common(select(Assessment.id), f, status_col=Assessment.status), f, Assessment.name)
    return lists._order(stmt, "assessments", f.sort, Assessment.id)


ASSESSMENT_COLUMNS = (
    Column("name", "Assessment", "string", Assessment.name),
    Column("status", "Status", "enum", Assessment.status),
    Column("delivery", "Delivery", "enum", Assessment.delivery),
    Column("time_limit_minutes", "Time limit (min)", "int", Assessment.time_limit_minutes),
    Column("pass_mark_pct", "Pass mark %", "decimal", Assessment.pass_mark_pct),
    Column("owner", "Owner", "string", _user_name(Assessment.owner_id)),
    Column("updated_at", "Updated", "datetime", Assessment.updated_at),
)


# ================================================================ campus drives / recruitment events (Epic 43)
async def _campus_drives(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.campus.common import drive_scope

    stmt = select(CampusDrive.id)
    cond = drive_scope(ctx)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _q(_common(stmt, f, status_col=CampusDrive.status), f, CampusDrive.name)
    return lists._order(stmt, "campus_drives", f.sort, CampusDrive.id)


DRIVE_COLUMNS = (
    Column("name", "Drive", "string", CampusDrive.name),
    Column("batch_year", "Batch", "int", CampusDrive.batch_year),
    Column("status", "Status", "enum", CampusDrive.status),
    Column("owner", "Owner", "string", _user_name(CampusDrive.owner_id)),
    Column("registration_opens_at", "Registration opens", "datetime", CampusDrive.registration_opens_at),
    Column("registration_closes_at", "Registration closes", "datetime", CampusDrive.registration_closes_at),
    Column("created_at", "Created", "datetime", CampusDrive.created_at),
)


async def _recruitment_events(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.core.permissions import scope_condition

    stmt = select(RecruitmentEvent.id)
    cond = scope_condition(
        principal,
        "event:manage" if principal.has("event:manage") else "campus:read",
        RecruitmentEvent.owner_id,
    )
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _q(_common(stmt, f, status_col=RecruitmentEvent.status), f, RecruitmentEvent.name)
    return lists._order(stmt, "recruitment_events", f.sort, RecruitmentEvent.id)


EVENT_COLUMNS = (
    Column("name", "Event", "string", RecruitmentEvent.name),
    Column("kind", "Kind", "enum", RecruitmentEvent.kind),
    Column("status", "Status", "enum", RecruitmentEvent.status),
    Column("starts_at", "Starts", "datetime", RecruitmentEvent.starts_at),
    Column("ends_at", "Ends", "datetime", RecruitmentEvent.ends_at),
    Column("venue", "Venue", "string", RecruitmentEvent.venue),
    Column("owner", "Owner", "string", _user_name(RecruitmentEvent.owner_id)),
)


# ================================================================ projects (Epic 45)
async def _projects(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.projects.common import scoped

    stmt = scoped(ctx, select(Project.id), "project:read")
    if not f.status:
        stmt = stmt.where(Project.archived_at.is_(None))
    stmt = _common(
        stmt,
        f,
        date_col=Project.start_date,
        le_col=Project.legal_entity_id,
        client_col=Project.client_id,
        status_col=Project.status,
    )
    stmt = _q(stmt, f, Project.name, Project.human_id)
    return lists._order(stmt, "projects", f.sort, Project.id)


def _budget_total() -> Any:
    return (
        select(func.sum(ProjectBudget.amount))
        .where(ProjectBudget.project_id == Project.id, ProjectBudget.dimension == "cost")
        .scalar_subquery()
    )


def _burn(attr: Any) -> Any:
    return (
        select(attr)
        .where(ProjectBurn.project_id == Project.id, ProjectBurn.dimension == "cost")
        .order_by(ProjectBurn.as_of.desc())
        .limit(1)
        .scalar_subquery()
    )


PROJECT_COLUMNS = (
    Column("human_id", "Project", "string", Project.human_id),
    Column("name", "Name", "string", Project.name),
    Column("client", "Client", "string", _client_name(Project.client_id)),
    Column("status", "Status", "enum", Project.status),
    Column("start_date", "Start", "date", Project.start_date),
    Column("end_date", "End", "date", Project.end_date),
    Column("owner", "Owner", "string", _user_name(Project.owner_id)),
    Column("budget", "Budget", "money", _budget_total(), Project.currency, compensation=True),
    Column("burn", "Burn (posted)", "money", _burn(ProjectBurn.actual), Project.currency, compensation=True),
    Column("burn_pct", "% used", "decimal", _burn(ProjectBurn.pct), compensation=True),
)


# ================================================================ immigration cases (Epic 46)
async def _immigration_cases(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.immigration.common import scoped

    stmt = scoped(ctx, select(ImmigrationCase.id))
    stmt = _common(stmt, f, status_col=ImmigrationCase.status)
    stmt = _q(stmt, f, ImmigrationCase.human_id)
    return lists._order(stmt, "immigration_cases", f.sort, ImmigrationCase.id)


IMMIGRATION_COLUMNS = (
    Column("human_id", "Case", "string", ImmigrationCase.human_id),
    Column(
        "candidate",
        "Person",
        "string",
        select(Candidate.full_name).where(Candidate.id == ImmigrationCase.candidate_id).scalar_subquery(),
        pii=True,
    ),
    Column(
        "visa_type",
        "Visa type",
        "string",
        select(VisaType.name).where(VisaType.id == ImmigrationCase.visa_type_id).scalar_subquery(),
    ),
    Column("country", "Country", "string", ImmigrationCase.country),
    Column("status", "Status", "enum", ImmigrationCase.status),
    Column("stage_key", "Stage", "enum", ImmigrationCase.stage_key),
    Column("receipt_number_last4", "Receipt (last 4)", "string", ImmigrationCase.receipt_number_last4),
    Column("owner", "Coordinator", "string", _user_name(ImmigrationCase.owner_user_id)),
    Column("created_at", "Opened", "datetime", ImmigrationCase.created_at),
)


# ================================================================ report definitions (Epic 47)
async def _report_definitions(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.reporting.builder.sharing import visible_condition

    stmt = select(ReportDefinition.id).where(
        ReportDefinition.archived_at.is_(None), visible_condition(principal)
    )
    stmt = _q(_common(stmt, f, status_col=ReportDefinition.status), f, ReportDefinition.name)
    return lists._order(stmt, "report_definitions", f.sort, ReportDefinition.id)


REPORT_COLUMNS = (
    Column("name", "Report", "string", ReportDefinition.name),
    Column("source_key", "Source", "string", ReportDefinition.source_key),
    Column("status", "Status", "enum", ReportDefinition.status),
    Column("owner", "Owner", "string", _user_name(ReportDefinition.owner_id)),
    Column("last_run_at", "Last run", "datetime", ReportDefinition.last_run_at),
    Column("updated_at", "Updated", "datetime", ReportDefinition.updated_at),
)


# ================================================================ referrals (Epic 42)
async def _referrals(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.referrals.common import scoped

    stmt = scoped(ctx, select(Referral.id).outerjoin(Demand, Demand.id == Referral.demand_id))
    stmt = _common(stmt, f, status_col=Referral.status)
    stmt = _q(stmt, f, Referral.human_id)
    return lists._order(stmt, "referrals", f.sort, Referral.id)


_RD = aliased(Demand)  # the list query outer-joins Demand for the scope; the label lookup needs its own alias


def _reward(attr: Any) -> Any:
    return (
        select(attr)
        .where(ReferralReward.referral_id == Referral.id)
        .order_by(ReferralReward.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )


REFERRAL_COLUMNS = (
    Column("human_id", "Referral", "string", Referral.human_id),
    Column(
        "demand", "Demand", "string", select(_RD.title).where(_RD.id == Referral.demand_id).scalar_subquery()
    ),
    Column("channel", "Channel", "enum", Referral.channel),
    Column("referrer_kind", "Referrer kind", "enum", Referral.referrer_kind),
    Column("status", "Status", "enum", Referral.status),
    Column("reward_status", "Reward", "enum", _reward(ReferralReward.status)),
    Column(
        "reward_amount",
        "Reward amount",
        "money",
        _reward(ReferralReward.amount),
        _reward(ReferralReward.currency),
        compensation=True,
    ),
    Column("created_at", "Submitted", "datetime", Referral.created_at),
)


# ================================================================ workforce plans (Epic 44)
async def _workforce_plans(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    from app.services.workforce.common import scoped

    stmt = scoped(ctx, select(WorkforcePlan.id), "workforce_plan:read")
    stmt = _common(
        stmt,
        f,
        date_col=WorkforcePlan.period_start,
        client_col=WorkforcePlan.client_id,
        status_col=WorkforcePlan.status,
    )
    stmt = _q(stmt, f, WorkforcePlan.name)
    return lists._order(stmt, "workforce_plans", f.sort, WorkforcePlan.id)


def _plan_budget() -> Any:
    return (
        select(func.sum(WorkforcePlanLine.budget_amount))
        .where(
            WorkforcePlanLine.workforce_plan_id == WorkforcePlan.id,
            WorkforcePlanLine.revision == WorkforcePlan.current_revision,
        )
        .scalar_subquery()
    )


def _plan_headcount() -> Any:
    return (
        select(func.coalesce(func.sum(WorkforcePlanLine.headcount), 0))
        .where(
            WorkforcePlanLine.workforce_plan_id == WorkforcePlan.id,
            WorkforcePlanLine.revision == WorkforcePlan.current_revision,
        )
        .scalar_subquery()
    )


PLAN_COLUMNS = (
    Column("name", "Plan", "string", WorkforcePlan.name),
    Column("client", "Client", "string", _client_name(WorkforcePlan.client_id)),
    Column("status", "Status", "enum", WorkforcePlan.status),
    Column("period_start", "From", "date", WorkforcePlan.period_start),
    Column("period_end", "To", "date", WorkforcePlan.period_end),
    Column("headcount", "Headcount", "int", _plan_headcount()),
    Column("budget", "Budget", "money", _plan_budget(), WorkforcePlan.currency, compensation=True),
    Column("owner", "Owner", "string", _user_name(WorkforcePlan.owner_id)),
)


KINDS: tuple[ListKindDef, ...] = (
    ListKindDef("talent_pools", P2ListFilters, POOL_COLUMNS, _talent_pools, "pool:read", TalentPool.id, "name"),
    ListKindDef("campaigns", P2ListFilters, CAMPAIGN_COLUMNS, _campaigns, "campaign:read", Campaign.id, "-created_at"),
    ListKindDef("assessments", P2ListFilters, ASSESSMENT_COLUMNS, _assessments, "assessment:read", Assessment.id, "-updated_at"),
    ListKindDef("campus_drives", P2ListFilters, DRIVE_COLUMNS, _campus_drives, "campus:read", CampusDrive.id, "-created_at"),
    ListKindDef("recruitment_events", P2ListFilters, EVENT_COLUMNS, _recruitment_events, "campus:read", RecruitmentEvent.id, "-starts_at"),
    ListKindDef("projects", P2ListFilters, PROJECT_COLUMNS, _projects, "project:read", Project.id, "human_id"),
    ListKindDef("immigration_cases", P2ListFilters, IMMIGRATION_COLUMNS, _immigration_cases, "immigration:read", ImmigrationCase.id, "-created_at"),
    ListKindDef("report_definitions", P2ListFilters, REPORT_COLUMNS, _report_definitions, "report:build", ReportDefinition.id, "-updated_at"),
    ListKindDef("referrals", P2ListFilters, REFERRAL_COLUMNS, _referrals, "referral:read", Referral.id, "-created_at"),
    ListKindDef("workforce_plans", P2ListFilters, PLAN_COLUMNS, _workforce_plans, "workforce_plan:read", WorkforcePlan.id, "-period_start"),
)  # fmt: skip
P3_KINDS = tuple(k.kind for k in KINDS)

for _k in KINDS:
    register_list_kind(_k)
