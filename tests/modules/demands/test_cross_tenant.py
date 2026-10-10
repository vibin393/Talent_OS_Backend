"""Every demand / approval / JD endpoint with tenant B ids returns 404 for tenant A."""

from typing import Any

from tests.modules.demands.helpers import (
    APPROVALS,
    CHAINS,
    DEMANDS,
    DOrg,
    create_demand,
    h,
    open_demand,
    role_id,
)


async def test_every_endpoint_404s_across_tenants(dorg: DOrg, other_dorg: DOrg) -> None:
    b = other_dorg.admin
    demand = await open_demand(b, other_dorg.client_id)
    draft = await create_demand(b, other_dorg.client_id)
    version = (
        await b.client.post(
            f"{DEMANDS}/{demand['id']}/jd-versions", json={"kind": "full", "content": "JD"}, headers=h(b)
        )
    ).json()
    chain = (
        await b.client.post(
            CHAINS,
            json={
                "subject_type": "demand",
                "name": "B chain",
                "steps": [{"name": "s", "approver_role_id": str(await role_id(other_dorg, "tenant_admin"))}],
            },
            headers=h(b),
        )
    ).json()
    await b.client.post(f"{DEMANDS}/{draft['id']}/submit", headers=h(b))
    request_id = (await b.client.get(f"{APPROVALS}/mine")).json()["items"][0]["id"]
    term = (
        await b.client.post(
            "/api/v1/settings/biased-terms", json={"term": "ninja", "category": "other"}, headers=h(b)
        )
    ).json()

    d, v = f"{DEMANDS}/{demand['id']}", version["id"]
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", d, None),
        ("PATCH", d, {"title": "x"}),
        ("POST", f"{d}/confirm-ai-fields", {}),
        ("POST", f"{d}/archive", None),
        ("POST", f"{DEMANDS}/{draft['id']}/submit", None),
        ("POST", f"{d}/transition", {"to": "on_hold", "reason_code": "other"}),
        ("GET", f"{d}/approvals", None),
        ("GET", f"{d}/documents", None),
        ("GET", f"{d}/jd-versions", None),
        ("POST", f"{d}/jd-versions", {"kind": "full", "content": "x"}),
        ("POST", f"{d}/jd-versions/generate", {"kind": "full"}),
        ("POST", f"{d}/jd-versions/{v}/restore", None),
        ("POST", f"{d}/jd-versions/{v}/bias-check", None),
        ("GET", f"{d}/posting", None),
        ("POST", f"{d}/posting", {"jd_version_id": v}),
        ("DELETE", f"{d}/posting", None),
        ("GET", f"{d}/assignments", None),
        ("PUT", f"{d}/assignments", {"user_ids": []}),
        ("GET", f"{APPROVALS}/{request_id}", None),
        ("POST", f"{APPROVALS}/{request_id}/decide", {"action": "approve"}),
        ("GET", f"{CHAINS}/{chain['id']}", None),
        (
            "PUT",
            f"{CHAINS}/{chain['id']}",
            {
                "subject_type": "demand",
                "name": "x",
                "steps": [{"name": "s", "approver_user_id": str(dorg.admin.user_id)}],
            },
        ),
        ("DELETE", f"{CHAINS}/{chain['id']}", None),
        ("DELETE", f"/api/v1/settings/biased-terms/{term['id']}", None),
    ]
    a = dorg.admin
    # Own demand + foreign version id -> 404 as well.
    own = await open_demand(a, dorg.client_id)
    calls.append(("POST", f"{DEMANDS}/{own['id']}/jd-versions/{v}/restore", None))
    calls.append(("POST", f"{DEMANDS}/{own['id']}/posting", {"jd_version_id": v}))
    for method, url, body in calls:
        resp = await a.client.request(method, url, json=body, headers=h(a))
        assert resp.status_code == 404, (method, url, resp.status_code, resp.text)
    # Tenant A's lists never contain tenant B rows.
    assert (await a.client.get(DEMANDS)).json()["total"] == 1
    assert (await a.client.get(f"{APPROVALS}/mine")).json()["total"] == 0
    # A foreign client in a body is an invalid reference.
    resp = await a.client.post(
        DEMANDS, json={"title": "x", "client_id": str(other_dorg.client_id)}, headers=h(a)
    )
    assert resp.status_code == 422
