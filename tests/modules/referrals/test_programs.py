# ruff: noqa: E501
"""Epic 42 story 42.1 (FR-213): programs (CRUD + validation), rule matching and priority, exclusions at referral
time (assigned recruiter / demand owner -> 409) and self-referral (422)."""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import FastAPI

from app.services.referrals import eligibility as el
from app.services.referrals.common import Referrer
from tests.modules.demands.helpers import DEMANDS, DOrg, h
from tests.modules.referrals.helpers import API, create_program, employee, program_body, refer, senior_demand

TID = uuid.uuid4()


def _program(**kw: object) -> el.ProgramFacts:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "active": True,
        "valid_from": date(2026, 1, 1),
        "valid_to": None,
        "eligible_referrers": "both",
        "eligible_demands": {"all": True},
        "reward_rules": [
            {"match": {"seniority_level": "senior"}, "amount": "25000.00", "currency": "INR"},
            {"match": {"employment_type": "contract"}, "amount": "15000.00", "currency": "INR"},
        ],
        "default_amount": Decimal("10000.00"),
        "currency": "INR",
        "joining_condition_days": 90,
        "exclusions": {"assigned_recruiters": True, "demand_owner": True, "excluded_role_keys": ["hr_onboarding"]},
        "priority": 0,
    }
    base.update(kw)
    return el.ProgramFacts(**base)  # type: ignore[arg-type]


def _demand(years: str | None = "8", etype: str | None = "permanent", **kw: object) -> el.DemandFacts:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "employment_type": etype,
        "min_experience_years": Decimal(years) if years else None,
        "owner_id": uuid.uuid4(),
    }
    base.update(kw)
    return el.DemandFacts(**base)  # type: ignore[arg-type]


def test_seniority_bands() -> None:
    assert [el.seniority_of(Decimal(v)) for v in ("0", "2.9", "3", "6.9", "7", "15")] == [
        "junior",
        "junior",
        "mid",
        "mid",
        "senior",
        "senior",
    ]
    assert el.seniority_of(None) is None


def test_reward_rule_first_match_else_default() -> None:
    p = _program()
    assert el.reward_amount(p, _demand("8", "contract")) == el.Reward(Decimal("25000.00"), "INR")  # first rule wins
    assert el.reward_amount(p, _demand("4", "contract")) == el.Reward(Decimal("15000.00"), "INR")
    assert el.reward_amount(p, _demand("4", "permanent")) == el.Reward(Decimal("10000.00"), "INR")
    assert el.reward_amount(_program(default_amount=None, reward_rules=[]), _demand()) is None


def test_program_for_priority_window_referrer_and_demand_filters() -> None:
    d = _demand()
    low = _program(priority=1)
    high = _program(priority=5)
    expired = _program(priority=9, valid_to=date(2026, 2, 1))
    workers_only = _program(priority=8, eligible_referrers="workers")
    other_client = _program(priority=7, eligible_demands={"client_ids": [str(uuid.uuid4())]})
    on = date(2026, 10, 6)
    assert el.program_for([low, high, expired, workers_only, other_client], d, "user", on) == high
    assert el.program_for([low, workers_only], d, "worker", on) == workers_only
    assert el.program_for([low], None, "user", on) is None  # general referral: no program
    assert el.program_for([_program(active=False)], d, "user", on) is None
    seniors = _program(eligible_demands={"seniority_levels": ["senior"], "employment_types": ["permanent"]})
    assert el.program_for([seniors], d, "user", on) == seniors
    assert el.program_for([seniors], _demand("2"), "user", on) is None


def test_exclusion_reasons() -> None:
    me = uuid.uuid4()
    referrer = Referrer("user", me, TID, "me@x.example.com", "Me", frozenset({"hr_onboarding"}))
    p = _program()
    assert el.exclusion_reason(p, _demand(assignee_user_ids=frozenset({me})), referrer) == "assigned_recruiter"
    assert el.exclusion_reason(p, _demand(owner_id=me), referrer) == "demand_owner"
    assert el.exclusion_reason(p, _demand(), referrer) == "excluded_role"
    clean = Referrer("user", me, TID, "me@x.example.com", "Me")
    assert el.exclusion_reason(p, _demand(), clean) is None
    worker = Referrer("worker", me, TID)
    assert el.exclusion_reason(p, _demand(owner_id=me), worker) is None


async def test_program_crud_and_validation(dorg: DOrg) -> None:
    p = await create_program(dorg)
    assert p["reward_rules"][0]["amount"] == "25000.00" and p["default_amount"] == "10000.00"
    dup = program_body(
        reward_rules=[
            {"match": {"seniority_level": "senior"}, "amount": "1.00", "currency": "INR"},
            {"match": {"seniority_level": "senior"}, "amount": "2.00", "currency": "INR"},
        ]
    )
    r = await dorg.admin.client.post(f"{API}/referral-programs", json=dup, headers=h(dorg.admin))
    assert r.status_code == 422
    for bad in (
        {"reward_rules": [{"match": {}, "amount": "1.00", "currency": "INR"}]},
        {"reward_rules": [{"match": {"seniority_level": "senior"}, "amount": "0", "currency": "INR"}]},
        {"currency": "RUPEES"},
        {"valid_to": "2025-01-01"},
    ):
        r = await dorg.admin.client.post(f"{API}/referral-programs", json=program_body(**bad), headers=h(dorg.admin))
        assert r.status_code == 422, bad
    upd = await dorg.admin.client.patch(
        f"{API}/referral-programs/{p['id']}", json={"priority": 3, "name": "Renamed"}, headers=h(dorg.admin)
    )
    assert upd.status_code == 200 and upd.json()["priority"] == 3
    bad_window = await dorg.admin.client.patch(
        f"{API}/referral-programs/{p['id']}", json={"valid_to": "2020-01-01"}, headers=h(dorg.admin)
    )
    assert bad_window.status_code == 422
    arch = await dorg.admin.client.post(f"{API}/referral-programs/{p['id']}/archive", headers=h(dorg.admin))
    assert arch.status_code == 200 and arch.json()["archived_at"] is not None
    listed = (await dorg.admin.client.get(f"{API}/referral-programs")).json()
    assert all(x["id"] != p["id"] for x in listed)
    s = await dorg.admin.client.put(
        f"{API}/referral-settings",
        json={"duplicate_activity_months": 4, "referee_link_days": 7, "share_link_days": 30},
        headers=h(dorg.admin),
    )
    assert s.status_code == 200 and s.json()["duplicate_activity_months"] == 4


async def test_configure_requires_permission(app: FastAPI, dorg: DOrg) -> None:
    emp = await employee(app, dorg)
    assert (await emp.client.get(f"{API}/referral-programs")).status_code == 403
    assert (await emp.client.get(f"{API}/referrals")).status_code == 403


async def test_assigned_recruiter_and_owner_refused_self_referral_422(app: FastAPI, dorg: DOrg) -> None:
    await create_program(dorg)
    demand = await senior_demand(dorg)
    rec = await employee(app, dorg, "Rita Recruiter", role="recruiter")
    r = await dorg.admin.client.put(
        f"{DEMANDS}/{demand['id']}/assignments",
        json={"user_ids": [str(rec.user_id)], "primary_user_id": str(rec.user_id)},
        headers=h(dorg.admin),
    )
    assert r.status_code == 200, r.text
    resp = await refer(rec.client, demand["id"], headers=rec.h())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "referrer_not_eligible"
    assert "sourcing" in resp.json()["error"]["message"]
    owner = await refer(dorg.admin.client, demand["id"], headers=h(dorg.admin))  # the admin owns the demand
    assert owner.status_code == 409 and owner.json()["error"]["code"] == "referrer_not_eligible"
    emp = await employee(app, dorg)
    me = await refer(emp.client, demand["id"], headers=emp.h(), email=emp.email.upper())
    assert me.status_code == 422 and me.json()["error"]["code"] == "self_referral"
