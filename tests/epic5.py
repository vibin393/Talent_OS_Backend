"""Shared helpers for the Epic 5 tests (search, embeddings, matching).

The test harness forces ``EMBEDDING_BACKEND=fake`` (tests/conftest.py): no model is ever downloaded. Celery
runs eagerly, so candidate / demand events refresh the search projection, embeddings and matches inline
before the request returns.
"""

import uuid
from typing import Any

import httpx

from app.core.context import RequestContext
from app.core.permissions import CATALOG, Principal, Scope
from tests.modules.identity.conftest import Member, csrf

CANDIDATES = "/api/v1/candidates"
SEARCH = "/api/v1/search/candidates"
GLOBAL = "/api/v1/search/global"
DEMANDS = "/api/v1/demands"


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


async def make_candidate(m: Member, full_name: str = "Rahul Sharma", **body: Any) -> dict[str, Any]:
    resp = await m.client.post(CANDIDATES, json={"full_name": full_name, **body}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def patch_candidate(m: Member, candidate_id: str, **body: Any) -> dict[str, Any]:
    resp = await m.client.patch(f"{CANDIDATES}/{candidate_id}", json=body, headers=h(m))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def search(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post(SEARCH, json=body, headers=csrf(client))


async def search_ok(m: Member, **body: Any) -> dict[str, Any]:
    resp = await search(m.client, **body)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def names(result: dict[str, Any]) -> list[str]:
    return [i["display_name"] for i in result["items"]]


def facet(result: dict[str, Any], key: str) -> dict[str, int]:
    for f in result["facets"]:
        if f["key"] == key:
            return {b["label"]: b["count"] for b in f["buckets"]}
    raise AssertionError(f"facet {key} missing")


def system_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> RequestContext:
    principal = Principal(
        type="user",
        id=user_id or uuid.uuid4(),
        tenant_id=tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=tenant_id, principal=principal)


DATA_ENGINEER: dict[str, Any] = {
    "current_title": "Data Engineer",
    "current_employer": "HDFC Bank",
    "location": {"city": "Chennai", "state": "Tamil Nadu", "country": "India"},
    "total_experience_months": 60,
    "notice_period_days": 30,
    "skills": [
        {"name": "Databricks", "years": 4},
        {"name": "PySpark", "years": 3},
        {"name": "ADF", "years": 2},
    ],
}
