"""Scope rules: recruiters see their own numbers, managers their BU, account managers their clients, and
another tenant's data is never counted or reachable (cross-tenant 404)."""

from tests.reporting.helpers import API, ROrg, tile
from tests.reporting.timeline import RANGE, build_march


async def test_cross_tenant_data_is_never_counted(rorg: ROrg, other_rorg: ROrg) -> None:
    await build_march(rorg)
    for key in ("open_demands", "time_to_fill", "db_sourced_submissions", "stage_conversion"):
        out = await tile(other_rorg.admin, "manager", key, **RANGE)
        assert out["status"] == "ok", key
        assert out["value"] in (0, None), (key, out["value"])
    throughput = await tile(other_rorg.admin, "manager", "throughput", **RANGE)
    assert all(p["value"] == 0 for s in throughput["series"] for p in s["points"])
    lst = await other_rorg.admin.client.get(f"{API}/reports/lists/submissions", params=RANGE)
    assert lst.json()["total"] == 0


async def test_cross_tenant_ids_in_list_filters_are_404(rorg: ROrg, other_rorg: ROrg) -> None:
    march = await build_march(rorg)
    for kind, params in (
        ("applications", {"demand_id": str(march.demands["D1"])}),
        ("submissions", {"client_id": str(march.c1)}),
        ("offers", {"demand_id": str(march.demands["D1"])}),
        ("interviews", {"demand_id": str(march.demands["D1"])}),
    ):
        resp = await other_rorg.admin.client.get(f"{API}/reports/lists/{kind}", params=params)
        assert resp.status_code == 404, (kind, resp.text)
        ok = await rorg.admin.client.get(f"{API}/reports/lists/{kind}", params=params)
        assert ok.status_code == 200, (kind, ok.text)


async def test_manager_sees_only_their_bu(rorg: ROrg) -> None:
    await build_march(rorg)
    manager = await rorg.manager.client.get(f"{API}/reports/lists/submissions", params=RANGE)
    admin = await rorg.admin.client.get(f"{API}/reports/lists/submissions", params=RANGE)
    assert manager.json()["total"] == 4  # s4 is in BU B
    assert admin.json()["total"] == 5
    bu_b = await tile(
        rorg.admin, "manager", "db_sourced_submissions", business_unit_ids=str(rorg.bu_b), **RANGE
    )
    assert bu_b["n"] == 1


async def test_recruiter_without_report_export_or_manager_dashboard(rorg: ROrg) -> None:
    resp = await rorg.recruiter.client.post(
        f"{API}/exports", json={"kind": "submissions", "format": "csv"}, headers=rorg.recruiter.h()
    )
    assert resp.status_code == 403
    assert (
        await rorg.recruiter.client.get(f"{API}/dashboards/account/tiles/offers_pending")
    ).status_code == 404
