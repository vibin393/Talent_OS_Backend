# ruff: file-ignore[line-too-long]
"""Domain-local referral events (Epic 42; the frozen contract event ``ReferralHired`` lives in
``app/events/referrals.py``). Ids and labels only, dispatched after commit; they drive the notification types
and the ``referrals.status_changed`` workflow trigger."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class ReferralStatusChanged:
    """Any status change. ``status_label`` is the referrer-safe label (the only value sent to the referrer)."""

    tenant_id: uuid.UUID
    referral_id: uuid.UUID
    from_status: str | None
    to_status: str
    status_label: str
    referrer_kind: str
    referrer_id: uuid.UUID
    demand_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ReferralReceived:
    """The referee accepted; recruiters on the Demand are told (``referral_received``)."""

    tenant_id: uuid.UUID
    referral_id: uuid.UUID
    demand_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ReferralDuplicateFound:
    tenant_id: uuid.UUID
    referral_id: uuid.UUID
    demand_id: uuid.UUID | None
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ReferralRewardEligible:
    tenant_id: uuid.UUID
    reward_id: uuid.UUID
    referral_id: uuid.UUID
    approval_request_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class ReferralRewardPaid:
    tenant_id: uuid.UUID
    reward_id: uuid.UUID
    referral_id: uuid.UUID
    referrer_kind: str
    referrer_id: uuid.UUID
