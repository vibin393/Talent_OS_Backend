# ruff: noqa: E501, D401
"""Shared helpers of the Epic 43 campus tests."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from tests.modules.demands.helpers import DOrg, h, open_demand

API = "/api/v1"
CAMPUS = f"{API}/campus"
PUBLIC = f"{API}/public/campus"
PROGRAMS = [
    {"name": "B.Tech CS", "branches": ["Computer Science"]},
    {"name": "B.Tech IT", "branches": ["Information Technology"]},
    {"name": "B.Tech ME", "branches": ["Mechanical"]},
]
RULE = {
    "and": [
        {"field": "program", "op": "in", "value": ["B.Tech CS", "B.Tech IT"]},
        {"field": "graduation_year", "op": "eq", "value": 2027},
        {"field": "cgpa", "op": "gte", "value": 7.0},
        {"field": "backlogs", "op": "eq", "value": 0},
    ]
}


def future(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def answers(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"program": "B.Tech CS", "branch": "Computer Science", "cgpa": "8.10", "backlogs": 0, "graduation_year": 2027}
    base.update(kw)
    return base


async def institution(org: DOrg, name: str | None = None, **kw: Any) -> dict[str, Any]:
    body = {"name": name or f"Institute {uuid.uuid4().hex[:6]}", "city": "Chennai", "tier": "tier_1", "programs": PROGRAMS, **kw}
    r = await org.admin.client.post(f"{CAMPUS}/institutions", json=body, headers=h(org.admin))
    assert r.status_code == 201, r.text
    return dict(r.json())


async def create_drive(org: DOrg, **kw: Any) -> dict[str, Any]:
    body = {"name": "Campus 2027", "batch_year": 2027, "registration_closes_at": future(), **kw}
    r = await org.admin.client.post(f"{CAMPUS}/drives", json=body, headers=h(org.admin))
    assert r.status_code == 201, r.text
    return dict(r.json())


async def ready_drive(org: DOrg, *, institutions: int = 1, rule: dict[str, Any] | None = None, demands: int = 1, **kw: Any) -> dict[str, Any]:
    """An open drive: N Institutions, demand(s), optional eligibility rule, registration open."""
    drive = await create_drive(org, **kw)
    for _ in range(institutions):
        inst = await institution(org)
        r = await org.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/institutions", json={"institution_id": inst["id"]}, headers=h(org.admin))
        assert r.status_code == 201, r.text
    for _ in range(demands):
        d = await open_demand(org.admin, org.client_id, positions=50, employment_type="permanent", rate_unit="year", pay_rate_min="500000.00", pay_rate_max="800000.00", bill_rate="900000.00")
        r = await org.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/demands", json={"demand_id": d["id"]}, headers=h(org.admin))
        assert r.status_code == 201, r.text
    if rule is not None:
        r = await org.admin.client.put(f"{CAMPUS}/drives/{drive['id']}/eligibility", json={"filters": rule}, headers=h(org.admin))
        assert r.status_code == 200, r.text
    r = await org.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/open-registration", headers=h(org.admin))
    assert r.status_code == 200, r.text
    return dict(r.json())


def token(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


async def register(client: Any, tok: str, *, email: str | None = None, name: str = "Ravi Kumar", ans: dict[str, Any] | None = None, **kw: Any) -> Any:
    view = await client.get(f"{PUBLIC}/r/{tok}")
    assert view.status_code == 200, view.text
    body: dict[str, Any] = {
        "full_name": name,
        "email": email or f"ravi-{uuid.uuid4().hex[:8]}@college.example.edu",
        "answers": ans if ans is not None else answers(),
        "notice_id": view.json()["notice"]["notice_id"],
        "agreed": True,
    }
    body.update(kw)
    return await client.post(f"{PUBLIC}/r/{tok}", json=body)


async def rows(org: DOrg, stmt: str, **params: Any) -> list[Any]:
    async with tenant_session(org.tenant_id) as db:
        return list((await db.execute(text(stmt), params)).all())


async def scalar(org: DOrg, stmt: str, **params: Any) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(text(stmt), params)).scalar()


async def execute(org: DOrg, stmt: str, **params: Any) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(text(stmt), params)


def ctx(org: DOrg, *, actor_kind: str | None = None, principal_type: str = "user") -> RequestContext:
    principal = Principal(type=principal_type, id=org.admin.user_id, tenant_id=org.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT))  # type: ignore[arg-type]
    return RequestContext(tenant_id=org.tenant_id, principal=principal, actor_kind=actor_kind)


async def registrations(org: DOrg, drive_id: str, **params: Any) -> list[dict[str, Any]]:
    r = await org.admin.client.get(f"{CAMPUS}/drives/{drive_id}/registrations", params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return list(r.json()["items"])
