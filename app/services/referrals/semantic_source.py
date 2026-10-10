# ruff: noqa: E501
"""Semantic sources ``referrals.referral`` and ``referrals.reward`` (Epic 42 A7, FR-237 "Referrals"), registered
from ``hooks.py``.

Scope ``referral:read`` through the Demand owner (as the internal referral views): the owner column is the
Demand's ``owner_id`` read through a correlated scalar subquery; general referrals (no Demand) are visible at
tenant scope only. The referee name is ``personal`` (permission ``referral:read``); the reward amount is
``compensation`` (masked without ``compensation:view``).
"""

from typing import Any

from sqlalchemy import select

from app.core.semantic import CMP, FieldDef, Op, ScopeSpec, register_semantic_source, sources
from app.models.referrals import (
    CHANNELS,
    INELIGIBLE_REASONS,
    PAYOUT_CHANNELS,
    REFERRAL_STATUSES,
    REFERRER_KINDS,
    REWARD_STATUSES,
    VOID_REASONS,
    Referral,
    ReferralReward,
)

REFERRAL_KEY = "referrals.referral"
REWARD_KEY = "referrals.reward"

_ID_OPS: frozenset[Op] = frozenset({"eq", "in"})
_ENUM_OPS: frozenset[Op] = frozenset({"eq", "in", "not_in"})
_DATE: frozenset[Op] = CMP | frozenset({"between", "within_days", "is_null"})
_TEXT: frozenset[Op] = frozenset({"contains", "starts_with", "eq"})
_HID: frozenset[Op] = frozenset({"eq", "in", "starts_with"})
_BOOL: frozenset[Op] = frozenset({"eq"})
_NUM: frozenset[Op] = CMP | frozenset({"between"})


def _demand_owner(demand_id: Any) -> Any:
    from app.models.demands import Demand

    return select(Demand.owner_id).where(Demand.id == demand_id).scalar_subquery()


def _enum(label: str, attr: str, values: tuple[str, ...]) -> FieldDef:
    return FieldDef(
        "enum", label, "none", lambda r: getattr(r, attr), _ENUM_OPS, enum_values=values, groupable=True
    )


def _uuid(label: str, attr: str) -> FieldDef:
    return FieldDef("uuid", label, "none", lambda r: getattr(r, attr), _ID_OPS, groupable=True)


def _when(label: str, attr: str) -> FieldDef:
    return FieldDef(
        "datetime", label, "none", lambda r: getattr(r, attr), _DATE, groupable=True, date_buckets=True
    )


def _referral_fields() -> dict[str, FieldDef]:
    return {
        "id": FieldDef("uuid", "Referral id", "none", lambda r: r.id, _ID_OPS),
        "human_id": FieldDef("string", "Referral number", "none", lambda r: r.human_id, _HID),
        "status": _enum("Status", "status", REFERRAL_STATUSES),
        "channel": _enum("Channel", "channel", CHANNELS),
        "referrer_kind": _enum("Referrer type", "referrer_kind", REFERRER_KINDS),
        "ineligible_reason": _enum("Not reward-eligible because", "ineligible_reason", INELIGIBLE_REASONS),
        "demand_id": _uuid("Demand", "demand_id"),
        "program_id": _uuid("Program", "program_id"),
        "referrer_user_id": _uuid("Referring employee", "referrer_user_id"),
        "candidate_id": _uuid("Candidate", "candidate_id"),
        "reward_eligible": FieldDef(
            "bool", "Reward eligible", "none", lambda r: r.reward_eligible, _BOOL, groupable=True
        ),
        "created_at": _when("Referred on", "created_at"),
        "accepted_at": _when("Accepted on", "accepted_at"),
        "decided_at": _when("Decided on", "decided_at"),
        "referee_name": FieldDef(
            "string",
            "Referred person",
            "personal",
            lambda r: r.referee_name,
            _TEXT,
            permission="referral:read",
        ),
    }


def _reward_fields() -> dict[str, FieldDef]:
    return {
        "id": FieldDef("uuid", "Reward id", "none", lambda r: r.id, _ID_OPS),
        "referral_id": _uuid("Referral", "referral_id"),
        "program_id": _uuid("Program", "program_id"),
        "status": _enum("Status", "status", REWARD_STATUSES),
        "payout_channel": _enum("Payout channel", "payout_channel", PAYOUT_CHANNELS),
        "void_reason": _enum("Void reason", "void_reason", VOID_REASONS),
        "referrer_kind": _enum("Referrer type", "referrer_kind", REFERRER_KINDS),
        "amount": FieldDef(
            "decimal",
            "Reward amount",
            "compensation",
            lambda r: r.amount,
            _NUM,
            aggs=frozenset({"sum", "avg", "min", "max"}),
        ),
        "currency": FieldDef("string", "Currency", "none", lambda r: r.currency, _ID_OPS, groupable=True),
        "condition_due_on": FieldDef(
            "date",
            "Condition due on",
            "none",
            lambda r: r.condition_due_on,
            _DATE,
            groupable=True,
            date_buckets=True,
        ),
        "eligible_at": _when("Eligible on", "eligible_at"),
        "approved_at": _when("Approved on", "approved_at"),
        "paid_at": _when("Paid on", "paid_at"),
        "created_at": _when("Created on", "created_at"),
    }


def register() -> None:
    """Register both sources once per process (safe to call again)."""
    have = sources()
    if REFERRAL_KEY not in have:
        register_semantic_source(
            REFERRAL_KEY,
            Referral,
            _referral_fields(),
            {},
            ScopeSpec(permission="referral:read", owner_cols=(lambda r: _demand_owner(r.demand_id),)),
            label="Referrals",
            subject=lambda r: r.id,
        )
    if REWARD_KEY not in have:

        def _reward_owner(r: Any) -> Any:
            return _demand_owner(
                select(Referral.demand_id).where(Referral.id == r.referral_id).scalar_subquery()
            )

        register_semantic_source(
            REWARD_KEY,
            ReferralReward,
            _reward_fields(),
            {},
            ScopeSpec(permission="referral:read", owner_cols=(_reward_owner,)),
            label="Referral rewards",
            subject=lambda r: r.id,
        )
