# ruff: noqa: E501
"""Epic 43 story 43.3: eligibility over ``campus.registration`` (first failing clause), rule changes, the star
override and the semantic source / campus fields."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.core import semantic as sem
from app.core.database import tenant_session
from app.services.campus import eligibility
from tests.modules.campus.helpers import (
    CAMPUS,
    RULE,
    answers,
    ctx,
    ready_drive,
    register,
    registrations,
    token,
)
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import new_client


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    client = new_client(app)
    yield client
    await client.aclose()


async def _students(anon: httpx.AsyncClient, tok: str) -> None:
    cases = {
        "ok@college.example.edu": answers(),
        "lowcgpa@college.example.edu": answers(cgpa="6.80"),
        "mech@college.example.edu": answers(program="B.Tech ME", branch="Mechanical"),
        "backlog@college.example.edu": answers(backlogs=2),
        "batch@college.example.edu": answers(graduation_year=2026, cgpa="5.0"),
    }
    for email, ans in cases.items():
        r = await register(anon, tok, email=email, ans=ans)
        assert r.status_code == 200, r.text


async def test_rules_mark_eligibility_with_first_failing_clause(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg, rule=RULE)
    await _students(anon, token(drive["institutions"][0]["registration_url"]))
    regs = {r["candidate_email"]: r for r in await registrations(dorg, drive["id"])}
    assert regs["ok@college.example.edu"]["status"] == "eligible"
    low = regs["lowcgpa@college.example.edu"]
    assert low["status"] == "not_eligible"
    assert low["eligibility"]["failing_rule"]["index"] == 2
    assert low["eligibility"]["failing_rule"]["label"] == "CGPA ≥ 7.0"
    assert regs["mech@college.example.edu"]["eligibility"]["failing_rule"]["field"] == "program"
    assert regs["backlog@college.example.edu"]["eligibility"]["failing_rule"]["label"] == "Active backlogs = 0"
    assert regs["batch@college.example.edu"]["eligibility"]["failing_rule"]["index"] == 1  # first failing, not cgpa


async def test_rule_change_reevaluates_and_override_survives(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg, rule=RULE)
    await _students(anon, token(drive["institutions"][0]["registration_url"]))
    regs = {r["candidate_email"]: r for r in await registrations(dorg, drive["id"])}
    mech = regs["mech@college.example.edu"]
    short = await dorg.admin.client.post(f"{CAMPUS}/registrations/{mech['id']}/override-eligibility", json={"reason": "no"}, headers=h(dorg.admin))
    assert short.status_code == 422
    ov = await dorg.admin.client.post(f"{CAMPUS}/registrations/{mech['id']}/override-eligibility", json={"reason": "Exceptional hackathon winner"}, headers=h(dorg.admin))
    assert ov.status_code == 200 and ov.json()["status"] == "eligible" and ov.json()["overridden"], ov.text
    again = await dorg.admin.client.post(f"{CAMPUS}/registrations/{mech['id']}/override-eligibility", json={"reason": "Exceptional hackathon winner"}, headers=h(dorg.admin))
    assert again.status_code == 409
    looser = {"and": [{"field": "cgpa", "op": "gte", "value": 6.5}]}
    r = await dorg.admin.client.put(f"{CAMPUS}/drives/{drive['id']}/eligibility", json={"filters": looser}, headers=h(dorg.admin))
    assert r.status_code == 200 and r.json()["eligibility_revision"] == 2
    regs = {x["candidate_email"]: x for x in await registrations(dorg, drive["id"])}
    assert regs["lowcgpa@college.example.edu"]["status"] == "eligible"
    assert regs["batch@college.example.edu"]["status"] == "not_eligible"
    assert regs["mech@college.example.edu"]["status"] == "eligible" and regs["mech@college.example.edu"]["overridden"]
    strict = {"field": "cgpa", "op": "gte", "value": 9.5}
    await dorg.admin.client.put(f"{CAMPUS}/drives/{drive['id']}/eligibility", json={"filters": strict}, headers=h(dorg.admin))
    regs = {x["candidate_email"]: x for x in await registrations(dorg, drive["id"])}
    assert regs["mech@college.example.edu"]["status"] == "eligible"  # override survives
    assert regs["ok@college.example.edu"]["status"] == "not_eligible"


@pytest.mark.parametrize(("actor_kind", "principal_type"), [("workflow", "user"), ("ai", "user"), ("system", "user"), (None, "api_key")])
async def test_override_is_human_only(dorg: DOrg, anon: httpx.AsyncClient, actor_kind: str | None, principal_type: str) -> None:
    from app.core.errors import Forbidden

    drive = await ready_drive(dorg, rule=RULE)
    await register(anon, token(drive["institutions"][0]["registration_url"]), ans=answers(cgpa="6.0"))
    reg = (await registrations(dorg, drive["id"]))[0]
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await eligibility.override(ctx(dorg, actor_kind=actor_kind, principal_type=principal_type), db, uuid.UUID(reg["id"]), "Automated override attempt")
    assert exc.value.code == "human_only_action"


async def test_unknown_field_and_bad_values_refused(dorg: DOrg) -> None:
    drive = await ready_drive(dorg)
    for filters, code in (
        ({"field": "caste", "op": "eq", "value": "x"}, "semantic_unknown_field"),
        ({"field": "cgpa", "op": "contains", "value": "7"}, "semantic_operator_not_allowed"),
        ({"field": "cgpa", "op": "gte", "value": "seven"}, "semantic_value_type"),
        ({"field": "candidate.full_name", "op": "eq", "value": "x"}, "semantic_unknown_field"),
    ):
        r = await dorg.admin.client.put(f"{CAMPUS}/drives/{drive['id']}/eligibility", json={"filters": filters}, headers=h(dorg.admin))
        assert r.status_code == 422 and r.json()["error"]["code"] in (code, "semantic_unknown_join"), (filters, r.text)


async def test_semantic_fuzz_never_errors_unexpectedly() -> None:
    import random

    from app.core.semantic import SemanticError

    src = sem.get_source("campus.registration")
    keys = [*src.fields, "nope", "institution.name", "drive_institution.status"]
    ops = ["eq", "ne", "in", "not_in", "lt", "gte", "between", "contains", "is_null", "skill_match", "within_days"]
    values: list[object] = [1, "x", 7.5, [1, 2], None, True, {"days": 3}, ["a", "b"], str(uuid.uuid4())]
    rnd = random.Random(43)
    for _ in range(300):
        cond = {"field": rnd.choice(keys), "op": rnd.choice(ops), "value": rnd.choice(values)}
        try:
            sem.validate({"source": "campus.registration", "filters": cond})
        except SemanticError as exc:
            assert exc.code.startswith("semantic_")


async def test_campus_batch_field_on_candidate_source(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    drive = await ready_drive(dorg)
    await register(anon, token(drive["institutions"][0]["registration_url"]), email="batch27@college.example.edu")
    assert "campus_batch" in sem.get_source("candidates.candidate").fields
    async with tenant_session(dorg.tenant_id) as db:
        sctx = sem.SemanticContext.for_system(dorg.tenant_id, db, "segment")
        hit = await sem.ids(sem.validate({"source": "candidates.candidate", "filters": {"field": "campus_batch", "op": "eq", "value": "2027"}}), sctx)
        miss = await sem.ids(sem.validate({"source": "candidates.candidate", "filters": {"field": "campus_batch", "op": "eq", "value": "2026"}}), sctx)
        many = await sem.ids(sem.validate({"source": "candidates.candidate", "filters": {"field": "campus_registrations.batch_year", "op": "eq", "value": 2027}}), sctx)
    reg = (await registrations(dorg, drive["id"]))[0]
    assert [str(i) for i in hit] == [reg["candidate_id"]] and miss == [] and [str(i) for i in many] == [reg["candidate_id"]]
    templates = await dorg.admin.client.get("/api/v1/talent-pools/templates")
    if templates.status_code == 200:
        campus = next(t for t in templates.json() if t["key"] == "campus")
        assert campus["available"] is True
