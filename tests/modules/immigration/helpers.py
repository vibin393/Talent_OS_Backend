"""Shared helpers for the Epic 46 immigration tests."""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from tests.epic3 import make_pdf
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import create_candidate

IMM = "/api/v1/immigration"
WORK = "/api/v1/portal/work/immigration"
PDF = make_pdf("immigration scan")
TODAY = date.today()


async def adopt(org: DOrg, key: str = "ae_employment", **body: Any) -> dict[str, Any]:
    resp = await org.admin.client.post(f"{IMM}/visa-templates/{key}/adopt", json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def open_case(
    org: DOrg, visa_type_id: str, candidate_id: uuid.UUID | None = None, **body: Any
) -> dict[str, Any]:
    cand = candidate_id or await create_candidate(org.admin, f"Ira Immigrant {uuid.uuid4().hex[:4]}")
    payload = {"candidate_id": str(cand), "visa_type_id": visa_type_id, **body}
    resp = await org.admin.client.post(f"{IMM}/cases", json=payload, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def post(org: DOrg, path: str, body: Any = None) -> Any:
    return await org.admin.client.post(f"{IMM}{path}", json=body, headers=h(org.admin))


def permit_body(**kw: Any) -> dict[str, Any]:
    base = {
        "number": "784-1987-1234567",
        "valid_from": TODAY.isoformat(),
        "valid_to": (TODAY + timedelta(days=700)).isoformat(),
    }
    base.update(kw)
    return base


def ctx(
    org: DOrg, *, scope: Scope = Scope.TENANT, user_id: uuid.UUID | None = None, **kw: Any
) -> RequestContext:
    perms = dict.fromkeys(CATALOG, Scope.TENANT)
    for k in ("immigration:read", "immigration:manage"):
        perms[k] = scope
    principal = Principal(
        type="user", id=user_id or org.admin.user_id, tenant_id=org.tenant_id, permissions=perms
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal, **kw)


async def sql(org: DOrg, stmt: str, **params: Any) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return (
            (await db.execute(text(stmt), params)).all()
            if stmt.lstrip().upper().startswith("SELECT")
            else (await db.execute(text(stmt), params))
        )
