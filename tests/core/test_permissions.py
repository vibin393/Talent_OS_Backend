"""Story 1.5: permission catalog, scopes, Principal checks, scope conditions and compensation masking."""

import re
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Column, MetaData, Table, Uuid, false, select
from sqlalchemy.dialects import postgresql

from app.core.errors import Forbidden
from app.core.permissions import (
    ALL_PERMISSIONS,
    CATALOG,
    DEFAULT_ROLE_KEYS,
    DEFAULT_ROLES,
    TENANT_ADMIN,
    InvalidGrant,
    Principal,
    Scope,
    check,
    mask_compensation,
    merge_grants,
    require,
    scope_condition,
    scope_filter,
    validate_grants,
)

_items = Table(
    "items", MetaData(), Column("id", Uuid()), Column("owner_id", Uuid()), Column("business_unit_id", Uuid())
)


def _principal(**perms: Scope) -> Principal:
    return Principal(
        type="user",
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        permissions={k.replace("__", ":"): v for k, v in perms.items()},
        team_ids=frozenset({uuid.uuid4()}),
        business_unit_id=uuid.uuid4(),
    )


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------- catalog
def test_catalog_keys_are_resource_action() -> None:
    assert all(re.fullmatch(r"[a-z_]+:[a-z_]+", key) for key in CATALOG)
    resources = {key.split(":")[0] for key in CATALOG}
    expected = {
        "client", "contact", "demand", "candidate", "application", "submission", "interview",
        "scorecard", "offer", "task", "report", "user", "role", "team", "business_unit", "settings",
        "audit", "ai", "privacy", "compensation",
    }  # fmt: skip
    assert expected <= resources
    assert {"demand:approve", "report:export", "compensation:view", "ai:admin"} <= ALL_PERMISSIONS


def test_scope_order() -> None:
    assert [s.rank for s in (Scope.OWN, Scope.TEAM, Scope.BU, Scope.TENANT)] == [0, 1, 2, 3]
    assert Scope.TENANT.covers(Scope.OWN)
    assert not Scope.TEAM.covers(Scope.BU)


def test_default_roles_are_valid_and_complete() -> None:
    assert {
        "tenant_admin", "recruitment_manager", "delivery_manager", "account_manager",
        "recruiter", "interviewer", "viewer", "grievance_officer",
        # Epic 14 (FR-81): the six Phase 2 default roles.
        "sales", "hr_onboarding", "compliance", "finance", "finance_admin", "vendor_manager",
        # Phase 3 (Epics 46 / 43 / 39): immigration, campus and recruitment-marketing default roles.
        "immigration_coordinator", "campus_recruiter", "recruitment_marketer",
        # Phase 4 (Epic 65): AI Administrator. Phase 5 (Epic 79): Integration Administrator.
        "ai_administrator", "integration_administrator",
    } == DEFAULT_ROLE_KEYS  # fmt: skip
    for template in DEFAULT_ROLES:
        validate_grants({k: v.value for k, v in template.permissions.items()})  # raises if illegal
    admin = next(t for t in DEFAULT_ROLES if t.key == TENANT_ADMIN)
    assert dict(admin.permissions) == dict.fromkeys(CATALOG, Scope.TENANT)
    viewer = next(t for t in DEFAULT_ROLES if t.key == "viewer")
    assert "compensation:view" not in viewer.permissions
    assert all(p.endswith(":read") for p in viewer.permissions)


@pytest.mark.parametrize(
    ("grants", "reason"),
    [
        ({"candidate:fly": "tenant"}, "unknown permission"),
        ({"candidate:read": "galaxy"}, "unknown scope"),
        ({"user:manage": "team"}, "not allowed"),
    ],
)
def test_validate_grants_rejects(grants: dict[str, str], reason: str) -> None:
    with pytest.raises(InvalidGrant) as exc:
        validate_grants(grants)
    assert reason in exc.value.reason


def test_merge_grants_keeps_widest_scope() -> None:
    merged = merge_grants(
        [
            {"candidate:read": "own", "task:read": "tenant"},
            {"candidate:read": "bu", "task:read": "team", "bogus:x": "tenant", "offer:read": "nope"},
        ]
    )
    assert merged == {"candidate:read": Scope.BU, "task:read": Scope.TENANT}


# ---------------------------------------------------------------- principal checks
def test_check_and_require() -> None:
    p = _principal(candidate__read=Scope.TEAM)
    check(p, "candidate:read")
    check(p, "candidate:read", Scope.TEAM)
    with pytest.raises(Forbidden) as exc:
        check(p, "candidate:read", Scope.TENANT)
    assert exc.value.code == "forbidden"
    assert exc.value.details["permission"] == "candidate:read"
    with pytest.raises(Forbidden):
        check(p, "user:read")
    with pytest.raises(ValueError, match="unknown permission"):
        require("candidate:teleport")
    with pytest.raises(ValueError, match="unknown permission"):
        check(p, "candidate:teleport")


# ---------------------------------------------------------------- scope conditions (SQL shape)
def test_scope_condition_per_scope() -> None:
    owner, bu = _items.c.owner_id, _items.c.business_unit_id
    tenant = _principal(candidate__read=Scope.TENANT)
    assert scope_condition(tenant, "candidate:read", owner) is None
    assert scope_filter(select(_items), tenant, "candidate:read", owner).whereclause is None

    missing = scope_condition(tenant, "offer:read", owner)
    assert missing is not None
    assert _sql(missing) == _sql(false())

    own = _sql(scope_condition(_principal(candidate__read=Scope.OWN), "candidate:read", owner))
    assert "team_members" not in own
    assert "items.owner_id = " in own

    team = _sql(scope_condition(_principal(candidate__read=Scope.TEAM), "candidate:read", owner))
    assert "team_members.team_id IN" in team
    assert "business_unit_id" not in team

    bu_with_col = _sql(scope_condition(_principal(candidate__read=Scope.BU), "candidate:read", owner, bu))
    assert "items.business_unit_id = " in bu_with_col
    bu_without_col = _sql(scope_condition(_principal(candidate__read=Scope.BU), "candidate:read", owner))
    assert "users.business_unit_id = " in bu_without_col


def test_scope_team_without_teams_is_own() -> None:
    p = Principal(type="user", id=uuid.uuid4(), permissions={"candidate:read": Scope.TEAM})
    sql = _sql(scope_condition(p, "candidate:read", _items.c.owner_id))
    assert "team_members" not in sql


# ---------------------------------------------------------------- compensation masking
class _Offer(BaseModel):
    title: str
    offered_ctc: Decimal
    bill_rate: Decimal | None = None


def test_mask_compensation_without_permission() -> None:
    viewer = _principal(offer__read=Scope.TENANT)
    data = {
        "title": "Engineer",
        "expected_ctc": {"amount": "1800000.00", "currency": "INR"},
        "margin": "12.5",
        "applications": [{"id": "a1", "pay_rate": "900"}, {"id": "a2"}],
    }
    masked = mask_compensation(data, viewer)
    assert masked == {
        "title": "Engineer",
        "expected_ctc": None,
        "margin": None,
        "applications": [{"id": "a1", "pay_rate": None}, {"id": "a2"}],
    }
    assert data["margin"] == "12.5"  # input untouched
    assert mask_compensation(_Offer(title="E", offered_ctc=Decimal(10)), viewer) == {
        "title": "E",
        "offered_ctc": None,
        "bill_rate": None,
    }
    assert mask_compensation({"salary": 1}, None) == {"salary": None}
    assert mask_compensation({"notes": "x"}, viewer, frozenset({"notes"})) == {"notes": None}


def test_mask_compensation_with_permission() -> None:
    finance = _principal(compensation__view=Scope.TENANT)
    data = {"title": "Engineer", "margin": "12.5"}
    assert mask_compensation(data, finance) is data
    assert mask_compensation(_Offer(title="E", offered_ctc=Decimal(10)), finance)["offered_ctc"] == Decimal(
        10
    )


def test_mask_compensation_matches_name_patterns() -> None:
    viewer = _principal(offer__read=Scope.TENANT)
    data = {
        "min_salary": 1,
        "max_budget": 2,
        "Expected_CTC": 3,
        "pay_rate_hourly": 4,
        "target_margin_pct": 5,
        "joining_bonus": 6,
        "total_compensation": 7,
        "max_rate": 8,
        "rating": 9,
        "contact": "c",
        "name": "n",
    }
    masked = mask_compensation(data, viewer)
    assert {k for k, v in masked.items() if v is None} == set(data) - {"rating", "contact", "name"}


def test_mask_compensation_rejects_unsupported_types() -> None:
    viewer = _principal(offer__read=Scope.TENANT)

    class Row:
        salary = 10

    with pytest.raises(TypeError):
        mask_compensation(Row(), viewer)
    with pytest.raises(TypeError):
        mask_compensation({"offer": Row()}, viewer)
    with pytest.raises(TypeError):
        mask_compensation("salary", _principal(compensation__view=Scope.TENANT))
    assert mask_compensation({"at": None, "n": Decimal(1), "id": uuid.uuid4()}, viewer)["n"] == Decimal(1)
