# ruff: noqa: E501
"""Epic 42 story 42.3 (FR-215): referrer-safe tracking -- the four labels for every status, the schema proof that
feedback / stage / reason / compensation fields are absent, the status handlers (rejection -> Not selected with no
reason), the Worker portal with org RLS (Worker A never sees Worker B's referrals -> 404), internal views and
cross-tenant isolation."""

import types
import typing
import uuid
from typing import Any, get_args, get_origin

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.field_visibility import registered_schemas
from app.models.referrals import REFERRAL_STATUSES
from app.schemas.referrals_referrer import (
    FORBIDDEN,
    ReferrerOpeningOut,
    ReferrerReferralOut,
    ReferrerRewardOut,
    ReferrerShareLinkOut,
    status_label,
)
from tests.modules.demands.helpers import DOrg, h
from tests.modules.pipeline.helpers import move, stages
from tests.modules.placements.helpers import direct_placement
from tests.modules.referrals.helpers import (
    API,
    ME,
    WORK,
    accept,
    create_program,
    employee,
    refer,
    refer_ok,
    referee_token,
    rows,
    scalar,
    senior_demand,
)
from tests.p2 import sign_in_candidate

EXPECTED = {
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


@pytest.mark.parametrize("status", REFERRAL_STATUSES)
def test_every_status_maps_to_one_of_four_labels(status: str) -> None:
    assert status_label(status) == EXPECTED[status]
    assert set(EXPECTED.values()) == {"Received", "In process", "Hired", "Not selected"}


def _field_names(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = seen or set()
    if model in seen:
        return set()
    seen.add(model)
    out: set[str] = set()
    for name, f in model.model_fields.items():
        out.add(name)
        stack: list[Any] = [f.annotation]
        while stack:
            tp = stack.pop()
            if isinstance(tp, type) and issubclass(tp, BaseModel):
                out |= _field_names(tp, seen)
            elif get_origin(tp) in (typing.Union, types.UnionType, list, tuple, dict) or get_args(tp):
                stack.extend(get_args(tp))
    return out


@pytest.mark.parametrize("model", [ReferrerReferralOut, ReferrerRewardOut, ReferrerOpeningOut, ReferrerShareLinkOut])
def test_referrer_schema_has_no_feedback_stage_reason_or_compensation_fields(model: type[BaseModel]) -> None:
    names = _field_names(model)
    offending = sorted(n for n in names for stem in FORBIDDEN if stem in n.lower())
    assert offending == [], offending
    assert {"feedback", "scorecard", "stage", "reason", "ctc", "salary", "compensation"} <= FORBIDDEN
    assert "worker" in registered_schemas()[model]


async def test_rejection_shows_not_selected_without_reason(app: FastAPI, dorg: DOrg) -> None:
    await create_program(dorg)
    emp = await employee(app, dorg)
    demand = await senior_demand(dorg)
    email = f"rej-{uuid.uuid4().hex[:6]}@friends.example.com"
    out = await refer_ok(emp, demand["id"], email=email)
    assert (await accept(emp.client, referee_token(email))).status_code == 200
    app_id = await scalar(dorg, "SELECT application_id FROM referrals WHERE id = :i", i=out["id"])
    ids = await stages(dorg.admin)
    r = await move(dorg.admin, str(app_id), ids["rejected"], reason_code="other", note="Not a fit for the client")
    assert r.status_code == 200, r.text
    assert await scalar(dorg, "SELECT status FROM referrals WHERE id = :i", i=out["id"]) == "not_selected"
    resp = await emp.client.get(ME)
    mine = resp.json()
    assert mine[0]["status_label"] == "Not selected"
    flat = resp.text.lower()
    for word in ("not a fit", "rejected", "reason", "stage", "feedback"):
        assert word not in flat
    internal = (await dorg.admin.client.get(f"{API}/referrals/{out['id']}")).json()
    assert internal["status"] == "not_selected" and internal["referrer_name"] == "Esha Employee"
    by_cand = (await dorg.admin.client.get(f"{API}/referrals/by-candidate/{internal['candidate_id']}")).json()
    assert by_cand[0]["referrer_name"] == "Esha Employee"
    listed = (await dorg.admin.client.get(f"{API}/referrals", params={"status": "not_selected"})).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == out["id"]
    resume = await dorg.admin.client.get(f"{API}/referrals/{out['id']}/resume")
    assert resume.status_code == 200 and resume.content[:4] == b"%PDF"


async def test_worker_portal_refer_and_org_rls(app: FastAPI, dorg: DOrg) -> None:
    await create_program(dorg)
    demand = await senior_demand(dorg)
    pa = await direct_placement(dorg)
    pb = await direct_placement(dorg)
    a = await sign_in_candidate(app, uuid.UUID(pa["candidate_id"]), dorg.tenant_id)
    b = await sign_in_candidate(app, uuid.UUID(pb["candidate_id"]), dorg.tenant_id)
    try:
        openings = await a.get(f"{WORK}/openings")
        assert openings.status_code == 200, openings.text
        mine_card = next(o for o in openings.json() if o["demand_id"] == demand["id"])
        assert mine_card["reward_amount"] == "25000.00" and "bill_rate" not in mine_card
        made = await refer(a, demand["id"], url=WORK, name="Farah Friend")
        assert made.status_code == 201, made.text
        rid = made.json()["id"]
        listed = (await a.get(WORK)).json()
        assert [x["id"] for x in listed] == [rid] and listed[0]["demand_title"] == demand["title"]
        assert (await b.get(WORK)).json() == []
        assert (await b.post(f"{WORK}/{rid}/withdraw", headers={"X-CSRF-Token": "csrf-token"})).status_code == 404
        link = await a.post(f"{WORK}/share-links", json={"demand_id": demand["id"]}, headers={"X-CSRF-Token": "csrf-token"})
        assert link.status_code == 200 and "/l/" in link.json()["url"]
        kind = (await rows(dorg, "SELECT referrer_kind, referrer_candidate_id FROM referrals WHERE id = :i", i=rid))[0]
        assert kind == ("worker", uuid.UUID(pa["candidate_id"]))
    finally:
        await a.aclose()
        await b.aclose()


async def test_non_worker_candidate_refused(app: FastAPI, dorg: DOrg) -> None:
    from tests.modules.offers.helpers import create_candidate

    cand = await create_candidate(dorg.admin, "Casey Candidate")
    c = await sign_in_candidate(app, cand, dorg.tenant_id)
    try:
        r = await c.get(f"{WORK}/openings")
        assert r.status_code == 403 and r.json()["error"]["code"] == "not_a_worker"
    finally:
        await c.aclose()


async def test_cross_tenant_404(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    await create_program(dorg)
    emp = await employee(app, dorg)
    demand = await senior_demand(dorg)
    out = await refer_ok(emp, demand["id"])
    assert (await other_dorg.admin.client.get(f"{API}/referrals/{out['id']}")).status_code == 404
    r = await other_dorg.admin.client.post(
        f"{API}/referrals/{out['id']}/link-demand", json={"demand_id": demand["id"]}, headers=h(other_dorg.admin)
    )
    assert r.status_code == 404
    assert (await other_dorg.admin.client.get(f"{API}/referrals")).json()["total"] == 0
