# ruff: file-ignore[line-too-long]
"""Referrer-safe referral payloads (Epic 42 A5, FR-215): the only shapes a referrer -- an internal User on
``/me/referrals`` or a Worker on ``/portal/work/referrals`` -- ever receives.

Four status labels only (``Received`` / ``In process`` / ``Hired`` / ``Not selected``) and the referrer's own
reward. Never interview feedback, scorecards, stage names, rejection reasons, offer / placement compensation,
rates or the Client contact. ``FORBIDDEN`` is proven absent by ``tests/modules/referrals/test_tracking.py``; the
models are also registered as worker portal schemas (FR-80 field-visibility walk).
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr

StatusLabel = Literal["Received", "In process", "Hired", "Not selected"]
RewardLabel = Literal["Pending", "Eligible", "Approved", "Paid", "Not payable"]

# Field-name stems never allowed in a referrer payload (spec A5).
FORBIDDEN: frozenset[str] = frozenset(
    {
        "feedback",
        "scorecard",
        "stage",
        "reason",
        "ctc",
        "salary",
        "rate",
        "bill_rate",
        "pay_rate",
        "compensation",
        "margin",
    }
)

STATUS_LABELS: dict[str, StatusLabel] = {
    "submitted": "Received",
    "accepted": "Received",
    "in_process": "In process",
    "hired": "Hired",
    "not_selected": "Not selected",
    "duplicate": "Not selected",
    "declined": "Not selected",
    "expired": "Not selected",
    "withdrawn": "Not selected",
}
REWARD_LABELS: dict[str, RewardLabel] = {
    "pending_condition": "Pending",
    "eligible": "Eligible",
    "approved": "Approved",
    "paid": "Paid",
    "void": "Not payable",
}


def status_label(status: str) -> StatusLabel:
    return STATUS_LABELS[status]


@register_portal_schema("worker")
class ReferrerRewardOut(BaseModel):
    status_label: RewardLabel
    amount: DecimalStr | None
    currency: str | None


@register_portal_schema("worker")
class ReferrerReferralOut(BaseModel):
    id: uuid.UUID
    human_id: str
    referee_name: str | None
    demand_title: str | None
    submitted_at: datetime
    status_label: StatusLabel
    can_withdraw: bool
    reward: ReferrerRewardOut | None


@register_portal_schema("worker")
class ReferrerOpeningOut(BaseModel):
    """Openings to refer for (careers job-card fields plus the referrer's own reward)."""

    demand_id: uuid.UUID
    title: str
    location: str | None
    employment_type: str | None
    work_mode: str | None
    client_name: str | None
    reward_amount: DecimalStr | None
    reward_currency: str | None


@register_portal_schema("worker")
class ReferrerShareLinkOut(BaseModel):
    url: str
    demand_id: uuid.UUID | None
    expires_at: datetime
