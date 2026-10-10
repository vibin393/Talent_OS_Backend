# ruff: noqa: E501
"""Epic 52 (A4): tile counts equal the owners' list endpoint totals under the same principal and filters (tenant scope
for the Tenant Admin, empty for a principal of another tenant)."""

from typing import Any

from tests.modules.demands.helpers import DOrg, h
from tests.modules.immigration.helpers import adopt, open_case

API = "/api/v1"


async def _tile(client: Any, dashboard: str, tile: str) -> dict[str, Any]:
    resp = await client.get(f"{API}/dashboards/{dashboard}/tiles/{tile}")
    assert resp.status_code == 200, resp.text
    body = dict(resp.json())
    assert body["status"] == "ok", body
    return body


async def test_immigration_open_cases_tile_equals_case_list(dorg: DOrg, other_dorg: DOrg) -> None:
    vt = await adopt(dorg)
    await open_case(dorg, vt["id"])
    await open_case(dorg, vt["id"])
    tile = await _tile(dorg.admin.client, "immigration", "cases_by_stage_category")
    listed = (await dorg.admin.client.get(f"{API}/immigration/cases", params={"status": "open_any"})).json()
    assert tile["value"] == len(listed) >= 2
    assert tile["link"]["query"] == {"status": "open_any"}
    other = await _tile(other_dorg.admin.client, "immigration", "cases_by_stage_category")
    assert other["value"] == 0


async def test_referral_funnel_equals_referral_list(dorg: DOrg) -> None:
    tile = await _tile(dorg.admin.client, "referrals", "referral_funnel")
    listed = (await dorg.admin.client.get(f"{API}/referrals", params={"limit": 100})).json()
    assert tile["value"] == listed["total"]


async def test_projects_list_kind_equals_project_list(dorg: DOrg) -> None:
    for name in ("Parity A", "Parity B"):
        resp = await dorg.admin.client.post(
            f"{API}/projects", json={"name": name, "client_id": str(dorg.client_id)}, headers=h(dorg.admin)
        )
        assert resp.status_code == 201
    report = (await dorg.admin.client.get(f"{API}/reports/lists/projects")).json()
    owner = (await dorg.admin.client.get(f"{API}/projects")).json()
    assert report["total"] == len(owner["items"] if isinstance(owner, dict) else owner) >= 2
