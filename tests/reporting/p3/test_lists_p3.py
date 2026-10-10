# ruff: noqa: E501
"""Epic 52 (A5, FR-262): the ten Phase 3 list kinds behind ``GET /reports/lists/{kind}`` and exports -- registered with
the owner read permission, compensation masked, immigration numbers only as last-4, no protected columns, export via
the P1 engine, cross-tenant isolation."""

import uuid
from typing import Any

from fastapi import FastAPI

from app.services.reporting import lists
from app.services.reporting.lists_p3 import KINDS, P3_KINDS
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import join

API = "/api/v1"
EXPECTED = {
    "talent_pools": "pool:read", "campaigns": "campaign:read", "assessments": "assessment:read",
    "campus_drives": "campus:read", "recruitment_events": "campus:read", "projects": "project:read",
    "immigration_cases": "immigration:read", "report_definitions": "report:build", "referrals": "referral:read",
    "workforce_plans": "workforce_plan:read",
}  # fmt: skip


def test_kinds_registered_with_owner_permissions() -> None:
    assert set(P3_KINDS) == set(EXPECTED)
    for kind, perm in EXPECTED.items():
        assert lists.READ_PERMISSION[kind] == perm


def test_no_protected_columns_and_masking_flags() -> None:
    for k in KINDS:
        keys = {c.key for c in k.columns}
        for stem in ("gender", "age_band", "disability", "self_id", "proctor", "answer", "fairness"):
            assert not any(stem in key for key in keys), (k.kind, stem)
        for c in k.columns:
            if c.type == "money":
                assert c.compensation, (k.kind, c.key)
    imm = {c.key for c in next(k for k in KINDS if k.kind == "immigration_cases").columns}
    assert "receipt_number_last4" in imm and "receipt_number" not in imm


async def test_every_kind_lists_for_the_admin(dorg: DOrg) -> None:
    for kind in P3_KINDS:
        resp = await dorg.admin.client.get(f"{API}/reports/lists/{kind}")
        assert resp.status_code == 200, (kind, resp.text)
        assert "total" in resp.json()


async def _project(org: DOrg, name: str) -> dict[str, Any]:
    resp = await org.admin.client.post(
        f"{API}/projects", json={"name": name, "client_id": str(org.client_id)}, headers=h(org.admin)
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_projects_list_parity_masking_and_isolation(dorg: DOrg, other_dorg: DOrg, app: FastAPI) -> None:
    project = await _project(dorg, f"List {uuid.uuid4().hex[:6]}")
    report = (await dorg.admin.client.get(f"{API}/reports/lists/projects", params={"page_size": 100})).json()
    owner = (await dorg.admin.client.get(f"{API}/projects")).json()
    owner_items = owner["items"] if isinstance(owner, dict) else owner
    assert report["total"] == len(owner_items)
    assert any(i["id"] == project["id"] for i in report["items"])
    other = (await other_dorg.admin.client.get(f"{API}/reports/lists/projects")).json()
    assert all(i["id"] != project["id"] for i in other["items"])

    finance = await join(app, dorg.tenant, "finance", name="Fin Ops")
    try:
        body = (await finance.client.get(f"{API}/reports/lists/projects")).json()
        masked = {c["key"] for c in body["columns"] if c["masked"]}
        assert {"budget", "burn", "burn_pct"} <= masked
        assert all(i.get("budget") is None for i in body["items"])
        recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
        try:
            assert (await recruiter.client.get(f"{API}/reports/lists/projects")).status_code == 403
        finally:
            await recruiter.client.aclose()
    finally:
        await finance.client.aclose()


async def test_projects_export_drops_compensation_columns(dorg: DOrg, app: FastAPI) -> None:
    await _project(dorg, f"Export {uuid.uuid4().hex[:6]}")
    finance = await join(app, dorg.tenant, "finance", name="Fin Ops")
    try:
        resp = await finance.client.post(
            f"{API}/exports", json={"kind": "projects", "format": "csv", "filters": {}}, headers=h(finance)
        )
        assert resp.status_code == 200, resp.text
        header = resp.text.splitlines()[0]
        assert "Budget" not in header and "Burn" not in header and "Project" in header
    finally:
        await finance.client.aclose()
