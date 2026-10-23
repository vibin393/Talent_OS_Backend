# ruff: file-ignore[line-too-long]
"""Code-defined feature specs (Epic 48 A3, FR-244). One spec per model kind and version.

A feature is ``{key, label, type numeric|binary|onehot, transform none|log1p|clip, clip?, categories?, source}``.
``label`` is the human-readable string used in contributions and in the AI explanation prompt. ``source`` names
the (aggregate) fact the extractor reads -- never a protected attribute, proxy, sub-city location, self-ID field
or free text (``denylist.py``; enforced by ``tests/modules/analytics/test_feature_denylist.py``).

Onehot features with ``categories=None`` learn their categories at training time (top ``max_categories`` by
frequency + ``other``); the learnt list is stored in the model ``params``.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any, Literal

FeatureType = Literal["numeric", "binary", "onehot"]
Transform = Literal["none", "log1p", "clip"]


@dataclasses.dataclass(frozen=True, slots=True)
class FeatureDef:
    key: str
    label: str
    type: FeatureType
    source: str
    transform: Transform = "none"
    clip: tuple[float, float] | None = None
    categories: tuple[str, ...] | None = None
    max_categories: int = 20
    # How the raw value is shown in contributions ("days", "ratio", "count", "pct_band", "flag", "category").
    display: str = "number"
    unit: str = ""

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "transform": self.transform,
            "source": self.source,
        }
        if self.clip is not None:
            out["clip"] = [repr(float(self.clip[0])), repr(float(self.clip[1]))]
        if self.categories is not None:
            out["categories"] = list(self.categories)
        return out


@dataclasses.dataclass(frozen=True, slots=True)
class FeatureSpec:
    kind: str
    version: int
    task: Literal["classification", "regression"]
    kind_label: str
    features: tuple[FeatureDef, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.features)

    def by_key(self) -> dict[str, FeatureDef]:
        return {f.key: f for f in self.features}

    def as_json(self) -> list[dict[str, Any]]:
        return [f.as_json() for f in self.features]


EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent", "freelance")

TIME_TO_FILL = FeatureSpec(
    "time_to_fill",
    1,
    "regression",
    "Time to fill",
    (
        FeatureDef(
            "required_skills", "Number of required skills", "numeric", "demand_skills.count", display="count"
        ),
        FeatureDef(
            "skill_rarity",
            "Rare skill combination",
            "numeric",
            "candidate_skills.inverse_share",
            "log1p",
            display="ratio",
        ),
        FeatureDef(
            "min_experience",
            "Minimum experience (years)",
            "numeric",
            "demand.min_experience_years",
            "clip",
            (0.0, 30.0),
            unit="years",
        ),
        FeatureDef("openings", "Number of openings", "numeric", "demand.positions", "log1p", display="count"),
        FeatureDef("priority", "Demand priority", "numeric", "demand.priority_rank", display="category"),
        FeatureDef(
            "employment_type",
            "Employment type",
            "onehot",
            "demand.employment_type",
            categories=EMPLOYMENT_TYPES,
            display="category",
        ),
        FeatureDef("remote", "Remote role", "binary", "demand.work_mode_remote", display="flag"),
        FeatureDef("city", "City", "onehot", "demand.city", display="category"),
        FeatureDef(
            "client_feedback_delay",
            "Client's typical feedback delay (days)",
            "numeric",
            "client_feedback.median_delay_12m",
            "clip",
            (0.0, 60.0),
            unit="days",
        ),
        FeatureDef(
            "client_fill_rate",
            "Client's prior fill rate",
            "numeric",
            "demands.client_fill_rate",
            display="pct",
        ),
        FeatureDef(
            "rate_vs_median",
            "Bill rate vs. typical rate",
            "numeric",
            "demand.bill_rate_over_median",
            "clip",
            (0.0, 3.0),
            display="ratio",
        ),
        FeatureDef(
            "recruiter_load",
            "Recruiter load (open demands)",
            "numeric",
            "demand_assignments.open_count",
            "log1p",
            display="count",
        ),
    ),
)

OFFER_DROP = FeatureSpec(
    "offer_drop",
    1,
    "classification",
    "Offer drop risk",
    (
        FeatureDef(
            "notice_period",
            "Notice period (days)",
            "numeric",
            "candidate.notice_period_days",
            "clip",
            (0.0, 180.0),
            unit="days",
        ),
        FeatureDef(
            "days_since_acceptance",
            "Days since acceptance",
            "numeric",
            "offer.accepted_at",
            "clip",
            (0.0, 180.0),
            unit="days",
        ),
        FeatureDef(
            "joining_lead_time",
            "Joining lead time (days)",
            "numeric",
            "offer.start_minus_acceptance",
            "clip",
            (0.0, 180.0),
            unit="days",
        ),
        FeatureDef(
            "offer_revisions",
            "Offer revisions",
            "numeric",
            "offer_versions.count",
            "clip",
            (0.0, 10.0),
            display="count",
        ),
        FeatureDef(
            "compensation_change",
            "Compensation change",
            "numeric",
            "offer.pay_vs_current_ctc_pct",
            "clip",
            (-50.0, 200.0),
            display="pct_band",
        ),
        FeatureDef(
            "prejoining_completion",
            "Pre-joining task completion",
            "numeric",
            "onboarding_tasks.done_ratio",
            display="pct",
        ),
        FeatureDef("bgv_pending", "Background check pending", "binary", "bgv_cases.pending", display="flag"),
        FeatureDef(
            "counter_offer", "Counter-offer recorded", "binary", "placement.counter_offer_at", display="flag"
        ),
        FeatureDef(
            "different_city",
            "Different city from work location",
            "binary",
            "candidate_city_vs_work_city",
            display="flag",
        ),
    ),
)

ATTRITION = FeatureSpec(
    "attrition",
    1,
    "classification",
    "Attrition risk",
    (
        FeatureDef("tenure", "Tenure (days)", "numeric", "placement.actual_start_date", "log1p", unit="days"),
        FeatureDef(
            "timesheet_rejections",
            "Timesheet rejections (last 90 days)",
            "numeric",
            "timesheets.rejected_90d",
            "clip",
            (0.0, 13.0),
            display="count",
        ),
        FeatureDef(
            "bench_entries",
            "Prior bench periods",
            "numeric",
            "bench_entries.count",
            "clip",
            (0.0, 10.0),
            display="count",
        ),
        FeatureDef(
            "pay_rate_vs_median",
            "Pay rate vs. typical rate",
            "numeric",
            "rate_segments.pay_over_median",
            "clip",
            (0.0, 3.0),
            display="ratio",
        ),
        FeatureDef(
            "leave_days",
            "Leave days taken (last 90 days)",
            "numeric",
            "leave_requests.days_90d",
            "clip",
            (0.0, 60.0),
            unit="days",
        ),
        FeatureDef(
            "pending_leave",
            "Pending leave requests",
            "numeric",
            "leave_requests.pending",
            "clip",
            (0.0, 10.0),
            display="count",
        ),
        FeatureDef(
            "prior_extensions",
            "Prior extensions",
            "numeric",
            "extensions.confirmed",
            "clip",
            (0.0, 10.0),
            display="count",
        ),
        FeatureDef(
            "client_early_end_rate",
            "Client's early-end rate",
            "numeric",
            "placements.client_early_end_rate",
            display="pct",
        ),
        FeatureDef(
            "days_to_planned_end",
            "Days to planned end",
            "numeric",
            "placement.planned_end_date",
            "clip",
            (-30.0, 730.0),
            unit="days",
        ),
    ),
)

SPECS: Mapping[str, FeatureSpec] = {s.kind: s for s in (TIME_TO_FILL, OFFER_DROP, ATTRITION)}
KIND_LABELS: Mapping[str, str] = {s.kind: s.kind_label for s in SPECS.values()}
SUBJECT_KINDS_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "time_to_fill": ("demand",),
    "offer_drop": ("offer", "placement"),
    "attrition": ("placement",),
}


def spec_for(kind: str) -> FeatureSpec:
    return SPECS[kind]
