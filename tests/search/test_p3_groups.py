# ruff: noqa: E501
"""Epic 52 (A5, FR-262): Phase 3 global-search groups delegate to the owners' ``quick_search`` functions with the
owners' scope; exact human ids pin first; cross-tenant searches are empty; principals without the group's read
permission and External Principals never get the group."""

import pytest
from fastapi import FastAPI

from app.core.hooks import autodiscover
from app.services.reporting import search_p3
from app.services.search import global_search as gs
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import join

API = "/api/v1"
P3_TYPES = (
    "talent_pool", "campaign", "assessment", "campus_drive", "recruitment_event", "project", "immigration_case",
    "report_definition",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _hooks() -> None:
    autodiscover("app.services")


def test_every_p3_group_is_registered() -> None:
    assert search_p3.PENDING_SEARCH_GROUPS == {}, "owner quick_search functions missing"
    assert set(P3_TYPES) <= set(gs.GLOBAL_GROUPS)
    for key in P3_TYPES:
        assert gs.GLOBAL_GROUPS[key].permission == search_p3.P3_SEARCH_GROUPS[key][1]


async def _project(org: DOrg, name: str) -> dict[str, str]:
    resp = await org.admin.client.post(
        f"{API}/projects", json={"name": name, "client_id": str(org.client_id)}, headers=h(org.admin)
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _search(client: object, q: str) -> dict[str, object]:
    resp = await client.get(f"{API}/search/global", params={"q": q})  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def test_project_exact_human_id_pinned_and_scoped(dorg: DOrg, other_dorg: DOrg, app: FastAPI) -> None:
    project = await _project(dorg, "Zephyr migration programme")
    out = await _search(dorg.admin.client, project["human_id"])
    assert out["exact"] is not None and out["exact"]["id"] == project["id"] and out["exact"]["exact"] is True  # type: ignore[index]
    groups = {g["type"]: g for g in out["groups"]}  # type: ignore[union-attr]
    assert set(P3_TYPES) <= set(groups), "the Tenant Admin holds every Phase 3 read permission"
    assert [i["id"] for i in groups["project"]["items"]] == [project["id"]]

    by_name = await _search(dorg.admin.client, "Zephyr migr")
    assert any(i["id"] == project["id"] for g in by_name["groups"] for i in g["items"])  # type: ignore[union-attr,index]

    # Cross-tenant: the other tenant never sees it.
    other = await _search(other_dorg.admin.client, project["human_id"])
    assert all(i["id"] != project["id"] for g in other["groups"] for i in g["items"])  # type: ignore[union-attr,index]
    assert other["exact"] is None or other["exact"]["id"] != project["id"]  # type: ignore[index]

    # A Recruiter has no project:read: no projects group at all.
    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        rec = await _search(recruiter.client, project["human_id"])
        types = {g["type"] for g in rec["groups"]}  # type: ignore[union-attr]
        assert "project" not in types and "immigration_case" not in types
    finally:
        await recruiter.client.aclose()


async def test_unauthenticated_search_refused(app: FastAPI) -> None:
    """Header search needs an internal User session (External Principals have none)."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(f"{API}/search/global", params={"q": "PRJ-000001"})
    assert resp.status_code in (401, 403)
