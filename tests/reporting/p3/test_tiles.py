# ruff: noqa: E501
"""Epic 52 (A4, FR-260): the eight Phase 3 dashboards -- listing, every tile computes, permission gating, the
supporting-signal badge, attrition gating, compensation masking and cross-tenant isolation."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.projects import ProjectBudget, ProjectBurn
from app.services.reporting.dashboards_p3 import P3_DASHBOARDS, P3_ORDER
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import join

API = "/api/v1"


async def _dashboards(client: Any) -> dict[str, Any]:
    resp = await client.get(f"{API}/dashboards")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"] if isinstance(body, dict) else body
    return {d["key"]: d for d in items}


async def _tile(client: Any, dashboard: str, tile: str, **params: str) -> Any:
    return await client.get(f"{API}/dashboards/{dashboard}/tiles/{tile}", params=params)


async def test_admin_gets_every_p3_dashboard_and_tile(dorg: DOrg) -> None:
    c = dorg.admin.client
    dashboards = await _dashboards(c)
    assert set(P3_ORDER) <= set(dashboards)
    for key in P3_ORDER:
        listed = {t["key"] for t in dashboards[key]["tiles"]}
        assert listed == {t.key for t in P3_DASHBOARDS[key].tiles}
        for tile in P3_DASHBOARDS[key].tiles:
            resp = await _tile(c, key, tile.key)
            assert resp.status_code == 200, (key, tile.key, resp.text)
            body = resp.json()
            assert body["status"] == "ok", (key, tile.key, body)
            if key == "predictions":
                assert body["badge"] == "Supporting signal"


async def test_gating_by_permission(dorg: DOrg, app: FastAPI) -> None:
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        keys = set(await _dashboards(recruiter.client))
        assert not keys & {"projects", "immigration", "predictions"}
        resp = await _tile(recruiter.client, "projects", "project_burn")
        assert resp.status_code == 404
    finally:
        await recruiter.client.aclose()


async def test_attrition_needs_read_attrition(dorg: DOrg, app: FastAPI) -> None:
    rm = await join(app, dorg.tenant, "recruitment_manager", name="Rana Manager")
    try:
        body = (await _tile(rm.client, "predictions", "risk_band_distribution")).json()
        assert body["status"] == "ok"
        assert all(r["key"] != "attrition" for r in body["rows"] or [])
    finally:
        await rm.client.aclose()
    admin = (await _tile(dorg.admin.client, "predictions", "risk_band_distribution")).json()
    assert any(r["key"] == "attrition" for r in admin["rows"])


async def _active_project_with_burn(org: DOrg, pct: str) -> dict[str, Any]:
    resp = await org.admin.client.post(
        f"{API}/projects",
        json={"name": f"Burn {uuid.uuid4().hex[:6]}", "client_id": str(org.client_id)},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    project = dict(resp.json())
    pid = uuid.UUID(project["id"])
    async with tenant_session(org.tenant_id) as db:
        from sqlalchemy import update

        from app.models.projects import Project

        await db.execute(update(Project).where(Project.id == pid).values(status="active", currency="INR"))
        db.add(
            ProjectBudget(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                project_id=pid,
                dimension="cost",
                amount=Decimal("1000.00"),
                currency="INR",
            )
        )
        db.add(
            ProjectBurn(
                id=uuid.uuid4(), tenant_id=org.tenant_id, project_id=pid, dimension="cost", actual=Decimal(pct) * 10,
                budget=Decimal("1000.00"), pct=Decimal(pct), burn_rate_per_day=Decimal("1.00"),
                forecast_at_completion=Decimal("1000.00"), currency="INR", as_of=date.today(), breakdown={}, source_hash="0" * 64,
            )
        )  # fmt: skip
    return project


async def test_project_money_masked_without_compensation(dorg: DOrg, app: FastAPI) -> None:
    project = await _active_project_with_burn(dorg, "85")
    admin = (await _tile(dorg.admin.client, "projects", "budget_threshold_breaches")).json()
    mine = [r for r in admin["rows"] if r["project"].startswith(project["human_id"])]
    assert mine and mine[0]["budget"] == {"amount": "1000.00", "currency": "INR"} and admin["masked"] is False
    finance = await join(app, dorg.tenant, "finance", name="Fin Ops")
    try:
        body = (await _tile(finance.client, "projects", "project_burn")).json()
        assert body["status"] == "ok" and body["masked"] is True
        row = next(r for r in body["rows"] if r["project"].startswith(project["human_id"]))
        assert "budget" not in row and "burn" not in row and row["pct_used"] == 85.0
    finally:
        await finance.client.aclose()


async def test_cross_tenant_tiles_are_isolated(dorg: DOrg, other_dorg: DOrg) -> None:
    project = await _active_project_with_burn(dorg, "90")
    other = (await _tile(other_dorg.admin.client, "projects", "project_burn")).json()
    assert other["status"] == "ok"
    assert all(link["path"] != f"/projects/{project['id']}" for link in other["row_links"] or [])
    mine = (await _tile(dorg.admin.client, "projects", "project_burn")).json()
    assert any(link["path"] == f"/projects/{project['id']}" for link in mine["row_links"])
