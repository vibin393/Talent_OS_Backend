# ruff: noqa: E501
"""Epic 44 story 44.5 (FR-226): Client Portal hiring forecasts (org RLS, permission, field whitelist, lock)."""

import uuid

from fastapi import FastAPI

from app.core.field_visibility import is_forbidden
from app.schemas.workforce_portal import ForecastLinePortalOut, ForecastPortalDetail, ForecastPortalOut
from tests.modules.commercials.helpers import COrg
from tests.modules.demands.helpers import create_client
from tests.modules.workforce.helpers import CP, WF, add_line, err, ok, plan, skill
from tests.p2 import External, make_client_user, sign_in_external

FORECAST = {
    "name": "H2 hiring",
    "period_start": "2027-04-01",
    "period_end": "2028-03-31",
    "lines": [
        {
            "role_title": "Java Developer",
            "skills": [{"name": "Java"}, {"name": "Underwater Basket Weaving"}],
            "headcount": 4,
            "location_text": "Pune",
            "planned_start_month": "2027-06",
            "notes": "Banking domain preferred",
        }
    ],
}


async def _cu(
    app: FastAPI,
    org: COrg,
    client_id: uuid.UUID | None = None,
    perms: tuple[str, ...] = ("forecasts:submit",),
) -> External:
    cid = client_id or org.client_id
    cu = await make_client_user(org.tenant_id, cid, permissions=perms)
    return await sign_in_external(app, "client_user", cu, org.tenant_id, cid)


def test_portal_schemas_whitelist() -> None:
    for model in (ForecastPortalOut, ForecastPortalDetail, ForecastLinePortalOut):
        for name in model.model_fields:
            assert not is_forbidden("client_user", name), (model.__name__, name)
            assert not any(word in name for word in ("budget", "rate", "approval", "owner", "notes")), name


async def test_client_forecast_flow(app: FastAPI, corg: COrg) -> None:
    await skill(corg, "Java")
    ext = await _cu(app, corg)
    try:
        created = await ok(await ext.client.post(f"{CP}/forecasts", json=FORECAST, headers=ext.h()), 201)
        assert created["status"] == "submitted" and created["editable"] and created["total_headcount"] == 4
        assert created["lines"][0]["comment"] == "Banking domain preferred"
        assert created["lines"][0]["planned_start_month"] == "2027-06"
        body = str(created)
        for word in ("budget", "bill_rate", "approval", "owner_id", "'notes'"):
            assert word not in body
        listed = await ok(await ext.client.get(f"{CP}/forecasts", headers=ext.h()))
        assert [f["id"] for f in listed] == [created["id"]]
        upd = await ok(
            await ext.client.patch(
                f"{CP}/forecasts/{created['id']}", json={"name": "H2 hiring v2"}, headers=ext.h()
            )
        )
        assert upd["name"] == "H2 hiring v2"
        # internal side: a Draft plan owned by the Account Manager (the client's owner = the admin here)
        p = await ok(await corg.c.get(f"{WF}/plans/{created['id']}", headers=corg.h()))
        assert (
            p["origin"] == "client_forecast"
            and p["status"] == "draft"
            and p["client_id"] == str(corg.client_id)
        )
        assert p["owner_id"] == str(corg.admin.user_id) and p["submitted_by_client_user_id"] == str(ext.id)
        (line,) = await ok(await corg.c.get(f"{WF}/plans/{created['id']}/lines", headers=corg.h()))
        assert line["skills"][1]["skill_key"] is None  # unmatched Client skill name kept until budgeting
        # budgeting needs every skill to match the catalogue and a budget on every line
        await ok(
            await corg.c.patch(
                f"{WF}/plans/{created['id']}/lines/{line['id']}",
                json={"budget_amount": "100000"},
                headers=corg.h(),
            )
        )
        e = err(
            await corg.c.post(f"{WF}/plans/{created['id']}/budget", headers=corg.h()),
            422,
            "skills_unresolved",
        )
        assert e["details"]["lines"][0]["role_title"] == "Java Developer"
        # the AM edited the plan -> locked for the Client
        err(
            await ext.client.patch(f"{CP}/forecasts/{created['id']}", json={"name": "x"}, headers=ext.h()),
            409,
            "forecast_locked",
        )
        err(
            await ext.client.delete(f"{CP}/forecasts/{created['id']}", headers=ext.h()),
            409,
            "forecast_locked",
        )
        got = await ok(await ext.client.get(f"{CP}/forecasts/{created['id']}", headers=ext.h()))
        assert got["status"] == "in_review" and not got["editable"]
        # a fresh forecast can be withdrawn while untouched
        second = await ok(await ext.client.post(f"{CP}/forecasts", json=FORECAST, headers=ext.h()), 201)
        assert (await ext.client.delete(f"{CP}/forecasts/{second['id']}", headers=ext.h())).status_code == 204
    finally:
        await ext.client.aclose()


async def test_permission_isolation_and_internal_plans(app: FastAPI, corg: COrg, other_corg: COrg) -> None:
    no_perm = await _cu(app, corg, perms=("projects:view",))
    a = await _cu(app, corg)
    client_b = await create_client(corg.admin, "Bravo Co")
    b = await _cu(app, corg, client_id=client_b)
    try:
        err(
            await no_perm.client.get(f"{CP}/forecasts", headers=no_perm.h()),
            403,
            "portal_permission_required",
        )
        err(
            await no_perm.client.post(f"{CP}/forecasts", json=FORECAST, headers=no_perm.h()),
            403,
            "portal_permission_required",
        )
        fa = await ok(await a.client.post(f"{CP}/forecasts", json=FORECAST, headers=a.h()), 201)
        # Client B cannot read / edit / delete Client A's forecast
        err(await b.client.get(f"{CP}/forecasts/{fa['id']}", headers=b.h()), 404, "not_found")
        err(
            await b.client.patch(f"{CP}/forecasts/{fa['id']}", json={"name": "x"}, headers=b.h()),
            404,
            "not_found",
        )
        err(await b.client.delete(f"{CP}/forecasts/{fa['id']}", headers=b.h()), 404, "not_found")
        assert await ok(await b.client.get(f"{CP}/forecasts", headers=b.h())) == []
        # an internal plan of Client A is invisible to Client A's users
        internal = await plan(corg, client_id=str(corg.client_id))
        await add_line(corg, internal["id"])
        assert [f["id"] for f in await ok(await a.client.get(f"{CP}/forecasts", headers=a.h()))] == [fa["id"]]
        err(await a.client.get(f"{CP}/forecasts/{internal['id']}", headers=a.h()), 404, "not_found")
        # internal routes refuse a client session
        resp = await a.client.get(f"{WF}/plans", headers=a.h())
        assert resp.status_code in (401, 403), resp.text
        # another tenant's internal user never sees the forecast
        err(await other_corg.c.get(f"{WF}/plans/{fa['id']}", headers=other_corg.h()), 404, "not_found")
        # validation: a line outside the period
        bad = {**FORECAST, "lines": [{**FORECAST["lines"][0], "planned_start_month": "2029-01"}]}  # type: ignore[dict-item]
        err(
            await a.client.post(f"{CP}/forecasts", json=bad, headers=a.h()), 422, "start_month_outside_period"
        )
    finally:
        for ext in (no_perm, a, b):
            await ext.client.aclose()


async def test_client_user_erasure_clears_reference(app: FastAPI, corg: COrg) -> None:
    from app.core.database import tenant_session
    from app.services.privacy.registry import ErasureContext
    from app.services.workforce_erasure import WorkforceClientRefsHandler

    ext = await _cu(app, corg)
    try:
        created = await ok(await ext.client.post(f"{CP}/forecasts", json=FORECAST, headers=ext.h()), 201)
    finally:
        await ext.client.aclose()
    handler = WorkforceClientRefsHandler()
    ctx = ErasureContext(
        tenant_id=corg.tenant_id, candidate_id=ext.id, run_id=uuid.uuid4(), reason="dsr",
        subject_kind="client_user", subject_id=ext.id,
    )  # fmt: skip
    async with tenant_session(corg.tenant_id) as db:
        assert await handler.verify(db, ctx)
        assert await handler.erase(db, ctx) == {"workforce_plans": 1}
        assert await handler.verify(db, ctx) == []
    p = await ok(await corg.c.get(f"{WF}/plans/{created['id']}", headers=corg.h()))
    assert p["submitted_by_client_user_id"] is None
