# ruff: file-ignore[line-too-long, lowercase-imported-as-non-lowercase]
"""Phase 3 dashboards (Epic 52, A4, FR-260): marketing, referrals, campus, mobility, skills, projects, immigration,
predictions.

Merged into the P1 ``DASHBOARDS`` registry (same tile mechanics as P1/P2: independent tiles, Redis cache, the P2
statement timeout ``DASHBOARD_P2_STATEMENT_TIMEOUT_MS``, ``timeout`` / ``unavailable``, drill-down links). Availability
is decided by **permissions** (``P3_GATES``) like Phase 2; internal Users only (dashboards need ``report:read`` and a
User session). Prediction tiles carry the "Supporting signal" badge (NFR-37); no tile shows self-ID or fairness data.
"""

from typing import Any

from app.core.permissions import Principal
from app.services.reporting import (
    p3tables as _P3,  # ruff: ignore[unused-import] - registers the optional-table guards  # noqa: F401
)
from app.services.reporting.dashboards import DashboardDef, TileDef, TileFn
from app.services.reporting.metrics import campus as _CA
from app.services.reporting.metrics import immigration as _IM
from app.services.reporting.metrics import marketing as _MK
from app.services.reporting.metrics import mobility as _MO
from app.services.reporting.metrics import predictions as _PR
from app.services.reporting.metrics import projects as _PJ
from app.services.reporting.metrics import referrals as _RF
from app.services.reporting.metrics import skills as _SK
from app.services.reporting.p2tables import needs as _need


def _p3(key: str, title: str, kind: str, size: str, desc: str, fn: TileFn, **kw: Any) -> TileDef:
    return TileDef(key, title, kind, size, desc, fn, **kw)


P3_DASHBOARDS: dict[str, DashboardDef] = {
    "marketing": DashboardDef("marketing", "Marketing", (
        _p3("campaign_funnel", "Campaign funnel", "funnel", "l",
            "Recipients, sent, delivered, clicked, applied and hired for campaign messages created in range. Opens are "
            "approximate and only counted where tracking consent allowed it.",
            _MK.campaign_funnel, requires=_need("campaign_messages", "engagement_events"), permissions=("campaign:read",)),
        _p3("suppressions_by_reason", "Suppressions by reason", "breakdown", "m",
            "Campaign messages suppressed in range, by reason.", _MK.suppressions_by_reason,
            requires=_need("campaign_messages"), permissions=("campaign:read",)),
        _p3("complaint_bounce_rate", "Complaints and bounces", "kpi", "m",
            "Complaint and bounce rate per channel in range; red above 0.1% complaints or 2% bounces (NFR-34).",
            _MK.complaint_bounce_rate, requires=_need("campaign_messages", "engagement_events"), permissions=("campaign:read",)),
        _p3("sequence_enrolments", "Sequence enrolments", "kpi", "s",
            "Sequence enrolments by status: active, completed, exited, suppressed.", _MK.sequence_enrolments,
            requires=_need("enrollments"), permissions=("campaign:read",)),
        _p3("campaign_attributed_applications", "Campaign-attributed applications", "kpi", "s",
            "Applications attributed to a campaign message (last touch within 30 days) in range; SM-22 per 1,000 delivered.",
            _MK.campaign_attributed_applications, requires=_need("campaign_attributions", "campaign_messages"),
            permissions=("campaign:read",)),
    )),
    "referrals": DashboardDef("referrals", "Referrals", (
        _p3("referral_funnel", "Referral funnel", "funnel", "l",
            "Referrals submitted in range you can see, by status.", _RF.referral_funnel,
            requires=_need("referrals"), permissions=("referral:read",)),
        _p3("referral_share_of_hires", "Referral share of hires", "kpi", "s",
            "SM-23: hires from referrals as a share of all hires in range.", _RF.referral_share_of_hires,
            requires=_need("referrals"), permissions=("referral:read",)),
        _p3("rewards_by_status", "Rewards by status", "breakdown", "m",
            "Referral rewards by status; amounts need compensation access.", _RF.rewards_by_status,
            requires=_need("referral_rewards", "referrals"), permissions=("referral:read",), compensation=True),
        _p3("top_referrers", "Top referrers", "table", "m",
            "Referrers with the most referrals in range and their hires.", _RF.top_referrers,
            requires=_need("referrals"), permissions=("referral:read",)),
    )),
    "campus": DashboardDef("campus", "Campus", (
        _p3("campus_funnel_by_institution", "Campus funnel by institution", "table", "l",
            "Registrations per institution by stage, including reneged offers.", _CA.campus_funnel_by_institution,
            requires=_need("registrations", "campus_drives"), permissions=("campus:read",)),
        _p3("drive_cycle_time", "Drive cycle time", "kpi", "s",
            "SM-25: median days from registration close to the first offer released.", _CA.drive_cycle_time,
            requires=_need("registrations", "campus_drives"), permissions=("campus:read",)),
        _p3("event_checkins_queue", "Event check-ins and queue", "kpi", "m",
            "Recruitment event check-ins, interviews done and the average queue wait in range.", _CA.event_checkins_queue,
            requires=_need("event_checkins", "queue_entries"), permissions=("campus:read",)),
    )),
    "mobility": DashboardDef("mobility", "Internal mobility", (
        _p3("internal_fill_rate", "Internal fill rate", "kpi", "s",
            "SM-24: positions filled through internal transfers as a share of all positions filled in range.",
            _MO.internal_fill_rate, requires=_need("internal_transfers"), permissions=("mobility:manage",)),
        _p3("internal_applications_by_status", "Internal applications", "breakdown", "m",
            "Internal applications submitted in range, by status.", _MO.internal_applications_by_status,
            requires=_need("mobility_applications"), permissions=("mobility:manage",)),
        _p3("transfers_confirmed", "Transfers confirmed", "series", "m",
            "Internal transfers confirmed per month.", _MO.transfers_confirmed,
            requires=_need("internal_transfers"), permissions=("mobility:manage",)),
    )),
    "skills": DashboardDef("skills", "Skills", (
        _p3("skills_supply_demand", "Skills supply and demand", "table", "l",
            "Top 20 skills: open positions requiring the skill vs people holding it (related skills included).",
            _SK.skills_supply_demand, permissions=("skills_insight:read",)),
        _p3("verified_skill_share", "Verified skills", "kpi", "s",
            "Share of candidate skills verified by a recruiter or an assessment.", _SK.verified_skill_share,
            requires=_need("candidate_skills"), permissions=("skills_insight:read",)),
    )),
    "projects": DashboardDef("projects", "Projects", (
        _p3("project_burn", "Project burn", "table", "l",
            "Active projects: cost budget, burn from posted facts and % used; money needs compensation access.",
            _PJ.project_burn, requires=_need("projects", "project_burn", "project_budgets"), permissions=("project:read",),
            compensation=True),
        _p3("deliverables_awaiting_acceptance", "Deliverables awaiting acceptance", "kpi", "s",
            "Deliverables submitted to the client and not yet accepted.", _PJ.deliverables_awaiting_acceptance,
            requires=_need("deliverables", "projects"), permissions=("project:read",)),
        _p3("budget_threshold_breaches", "Budget threshold breaches", "table", "m",
            "Active projects at or above 80% of their cost budget.", _PJ.budget_threshold_breaches,
            requires=_need("projects", "project_burn", "project_budgets"), permissions=("project:read",), compensation=True),
    )),
    "immigration": DashboardDef("immigration", "Immigration", (
        _p3("cases_by_stage_category", "Cases by stage", "breakdown", "m",
            "Open cases by stage category: not started, preparing, filed, pending decision.", _IM.cases_by_stage_category,
            requires=_need("immigration_cases"), permissions=("immigration:read",)),
        _p3("permits_expiring", "Permits expiring", "kpi", "m",
            "Current permits expiring within 30 / 60 / 90 days.", _IM.permits_expiring,
            requires=_need("permits", "immigration_cases"), permissions=("immigration:read",)),
        _p3("worker_days_without_permit", "Workers abroad without a permit", "kpi", "s",
            "SM-28 (target 0): Workers working abroad today without a valid permit.", _IM.worker_days_without_permit,
            requires=_need("immigration_cases", "permits"), permissions=("immigration:read",)),
    )),
    "predictions": DashboardDef("predictions", "Predictions", (
        _p3("risk_band_distribution", "Risk bands", "breakdown", "m",
            "Supporting signal: latest risk band per subject and model kind (attrition needs its own permission).",
            _PR.risk_band_distribution, requires=_need("ml_predictions"), permissions=("prediction:read",)),
        _p3("model_status", "Model status", "table", "m",
            "Supporting signal: each model kind's status, training date, sample sizes and metric vs baseline.",
            _PR.model_status, permissions=("prediction:read",)),
        _p3("high_risk_offers_with_intervention", "High-risk offers with intervention", "kpi", "s",
            "Supporting signal (SM-26): high drop-out risk offers that got an intervention task after the prediction.",
            _PR.high_risk_offers_with_intervention, requires=_need("ml_predictions"), permissions=("prediction:read",)),
    )),
}  # fmt: skip

P3_GATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "marketing": (("campaign:read",),),
    "referrals": (("referral:read",),),
    "campus": (("campus:read",),),
    "mobility": (("mobility:manage",),),
    "skills": (("skills_insight:read",),),
    "projects": (("project:read",),),
    "immigration": (("immigration:read",),),
    "predictions": (("prediction:read",),),
}
P3_ORDER = (
    "marketing",
    "referrals",
    "campus",
    "mobility",
    "skills",
    "projects",
    "immigration",
    "predictions",
)
P3_KEYS = frozenset(P3_ORDER)
SUPPORTING_SIGNAL_DASHBOARDS = frozenset({"predictions"})


def p3_allowed(principal: Principal, key: str) -> bool:
    from app.services.reporting.filters import holds

    return any(all(holds(principal, p) for p in group) for group in P3_GATES[key])


def p3_keys(principal: Principal) -> list[str]:
    """Phase 3 dashboards the caller may open (after the P1 / P2 ones)."""
    return [k for k in P3_ORDER if p3_allowed(principal, k)]
