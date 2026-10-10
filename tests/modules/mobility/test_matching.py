# ruff: noqa: E501
"""Epic 41 story 41.4 (FR-212): the ``internal`` match section (stored scores, ahead of the external list, absent for
``external`` demands, no recompute), Employee fit without compensation, eligibility pure rules, erasure."""

import uuid
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.services.mobility import eligibility
from app.services.mobility.employees import Employee
from app.services.mobility.matches import fit_of
from app.services.privacy.registry import ErasureContext
from tests.modules.demands.helpers import DOrg, h
from tests.modules.mobility.helpers import (
    WORK,
    M,
    active_worker,
    apply_worker,
    demand,
    employee,
    execute,
    scalar,
    store_match,
    worker,
)
from tests.modules.offers.helpers import create_candidate


def _emp(**kw: Any) -> Employee:
    base: dict[str, Any] = {
        "kind": "worker",
        "tenant_id": uuid.uuid4(),
        "user_id": None,
        "candidate_id": uuid.uuid4(),
        "placement_id": uuid.uuid4(),
        "tenure_start": date(2026, 1, 15),
        "grade": None,
        "location": None,
        "business_unit_id": None,
        "name": "W",
        "email": None,
    }
    base.update(kw)
    return Employee(**base)


def test_months_between_and_tenure_rule() -> None:
    assert eligibility.months_between(date(2026, 1, 15), date(2026, 4, 14)) == 2
    assert eligibility.months_between(date(2026, 1, 15), date(2026, 4, 15)) == 3
    assert eligibility.months_between(None, date(2026, 4, 15)) == 0
    rules = eligibility.Rules(min_tenure_months=6)
    res = eligibility.evaluate(rules, None, _emp(), date(2026, 4, 15))
    assert not res.eligible and "you have 3 months" in res.reasons[0]
    assert eligibility.evaluate(None, 3, _emp(), date(2026, 4, 15)).eligible
    assert not eligibility.evaluate(None, 4, _emp(), date(2026, 4, 15)).eligible


def test_grade_location_bu_rules() -> None:
    on = date(2027, 1, 1)
    r = eligibility.Rules(grades=("L3",), locations=("Chennai",), business_unit_ids=(uuid.uuid4(),))
    assert len(eligibility.evaluate(r, None, _emp(), on).reasons) == 2  # worker: grade + location (BU skipped)
    assert eligibility.evaluate(r, None, _emp(grade="l3", location="Chennai, Tamil Nadu"), on).eligible
    user = _emp(kind="user", user_id=uuid.uuid4(), grade="L3", location=None)
    res = eligibility.evaluate(r, None, user, on)
    assert res.reasons == ("Open to other business units only.",)  # user location unknown -> skipped


def test_fit_hides_compensation_and_ranks_skills() -> None:
    fit = fit_of(
        "81.6",
        [
            {"kind": "compensation", "text": "Expects 30 LPA", "comp": True},
            {"kind": "skill", "text": "4 yrs PySpark"},
            {"kind": "location", "text": "Based in Chennai"},
            {"kind": "skill", "text": "Python"},
        ],
        ["Azure"],
    )
    assert fit.fit_pct == 82
    assert fit.reasons == ("Matches 4 yrs PySpark", "Matches Python", "Missing Azure")
    assert all("LPA" not in r for r in fit.reasons)


async def test_internal_section_from_stored_scores(app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.matching import runs, service

    d = await demand(dorg, "both", title="Both Data Engineer")
    _, wid = await active_worker(dorg)
    outsider = await create_candidate(dorg.admin, "Ext Erna")
    await store_match(dorg, d["id"], wid, "70.0")
    await store_match(dorg, d["id"], outsider, "90.0")

    def no_recompute(*a: Any, **k: Any) -> Any:
        raise AssertionError("matching must not recompute for sections")

    monkeypatch.setattr(service, "request_recompute", no_recompute)
    for name in ("score_demand", "run_for_demand", "run_demand"):
        if hasattr(runs, name):
            monkeypatch.setattr(runs, name, no_recompute)

    r = await dorg.admin.client.get(f"/api/v1/demands/{d['id']}/matches")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["candidate"]["id"] for i in body["items"]] == [str(outsider), str(wid)]  # default list unchanged
    (section,) = body["sections"]
    assert (section["key"], section["label"], section["total"]) == ("internal", "Internal", 1)
    assert section["items"][0]["candidate"]["id"] == str(wid) and section["items"][0]["overall"] == 70.0

    ext = await demand(dorg, "external")
    await store_match(dorg, ext["id"], wid)
    assert (await dorg.admin.client.get(f"/api/v1/demands/{ext['id']}/matches")).json()["sections"] == []
    page2 = await dorg.admin.client.get(f"/api/v1/demands/{d['id']}/matches?page=2")
    assert page2.json()["sections"] is None

    # Rules exclude the worker -> the section is empty.
    await dorg.admin.client.put(f"{M}/demands/{d['id']}/rules", json={"min_tenure_months": 600}, headers=h(dorg.admin))
    (section,) = (await dorg.admin.client.get(f"/api/v1/demands/{d['id']}/matches")).json()["sections"]
    assert section["total"] == 0


async def test_employee_fit_on_board(app: FastAPI, dorg: DOrg) -> None:
    scored = await demand(dorg, "internal", title="Scored")
    unscored = await demand(dorg, "internal", title="Unscored")
    _, wid = await active_worker(dorg)
    await store_match(dorg, scored["id"], wid, "64.4")
    await execute(dorg, "DELETE FROM matches WHERE demand_id = :d", d=uuid.UUID(unscored["id"]))  # never scored
    w = await worker(app, dorg, wid)
    items = (await w.get(WORK)).json()["items"]
    assert [i["demand_id"] for i in items] == [scored["id"], unscored["id"]]  # fit desc, nulls last
    assert items[0]["fit_pct"] == 64 and items[0]["fit_reasons"][0] == "Matches 4 yrs PySpark"
    assert all("LPA" not in r for r in items[0]["fit_reasons"])
    assert items[1]["fit_pct"] is None and items[1]["fit_reasons"] == []
    emp = await employee(app, dorg)
    assert all(i["fit_pct"] is None for i in (await emp.client.get(f"{M}/openings")).json()["items"])


async def test_erasure_removes_mobility_rows(app: FastAPI, dorg: DOrg) -> None:
    from app.services.mobility_erasure import MobilityHandler

    d = await demand(dorg, "internal")
    _, wid = await active_worker(dorg)
    w = await worker(app, dorg, wid)
    assert (await apply_worker(w, d["id"])).status_code == 201
    await dorg.admin.client.put(f"{M}/profiles", json={"subject_kind": "worker", "subject_id": str(wid), "grade": "L2"}, headers=h(dorg.admin))
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=wid, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        assert (await MobilityHandler().export(db, ctx)) is not None
        counts = await MobilityHandler().erase(db, ctx)
        assert counts["mobility_applications"] == 1 and counts["mobility_profiles"] == 1
        assert await MobilityHandler().verify(db, ctx) == []
    assert await scalar(dorg, "SELECT count(*) FROM mobility_applications WHERE candidate_id = :c", c=wid) == 0
