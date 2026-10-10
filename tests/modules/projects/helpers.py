"""Shared helpers of the Epic 45 (SOW & project staffing) tests."""

import uuid
from datetime import date
from typing import Any

from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.placements import Placement
from tests.modules.commercials.helpers import (
    API,
    COrg,
    active_contract,
    create_contract,
    err,
    ok,
    upload_signed,
)
from tests.p2 import External, make_client_user, sign_in_external

CP = "/api/v1/client-portal"
PORTAL_PERMS = ("projects:view", "deliverables:accept", "invoices:view")

__all__ = ["API", "CP", "COrg", "err", "ok"]


async def active_sow(org: COrg, **overrides: Any) -> dict[str, Any]:
    msa = await active_contract(org)
    body = {
        "contract_type": "sow",
        "title": "SOW Delivery",
        "parent_contract_id": msa["id"],
        "pricing_model": "milestone",
        "value_cap": "1000000",
        **overrides,
    }
    sow = await create_contract(org, **body)
    await upload_signed(org, sow["id"])
    return dict(await ok(await org.c.post(f"{API}/contracts/{sow['id']}/activate", headers=org.h())))


async def milestone(org: COrg, sow_id: str, name: str = "Design", amount: str = "250000") -> dict[str, Any]:
    return dict(
        await ok(
            await org.c.post(
                f"{API}/contracts/{sow_id}/milestones", json={"name": name, "amount": amount}, headers=org.h()
            ),
            201,
        )
    )


async def project(org: COrg, sow_id: str | None = None, *, activate: bool = True, **body: Any) -> dict[str, Any]:
    payload = {"name": "Core banking rollout", "client_id": str(org.client_id), **body}
    if sow_id:
        payload["contract_id"] = sow_id
    prj = await ok(await org.c.post(f"{API}/projects", json=payload, headers=org.h()), 201)
    if activate:
        prj = await ok(
            await org.c.post(f"{API}/projects/{prj['id']}/transition", json={"to": "active"}, headers=org.h())
        )
    return dict(prj)


async def role(org: COrg, project_id: str, **body: Any) -> dict[str, Any]:
    payload = {"title": "Java Developer", "headcount": 1, **body}
    return dict(
        await ok(await org.c.post(f"{API}/projects/{project_id}/roles", json=payload, headers=org.h()), 201)
    )


async def candidate(org: COrg, name: str = "Priya Raman") -> uuid.UUID:
    from tests.epic3 import create_candidate

    return uuid.UUID((await create_candidate(org.admin, full_name=name))["id"])


async def placement(
    org: COrg, cand: uuid.UUID | None = None, *, client_id: uuid.UUID | None = None, status: str = "active"
) -> uuid.UUID:
    cand = cand or await candidate(org)
    pid = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(
            Placement(
                id=pid,
                tenant_id=org.tenant_id,
                human_id=f"PLC-{uuid.uuid4().hex[:6]}",
                candidate_id=cand,
                client_id=client_id or org.client_id,
                legal_entity_id=org.entity_id,
                worker_type="employee",
                payee_kind="worker",
                planned_start_date=date(2026, 1, 1),
                currency="INR",
                status=status,
            )
        )
    return pid


async def assign(
    org: COrg, role_id: str, placement_id: uuid.UUID, pct: str = "100", start: str = "2026-02-01", **extra: Any
) -> Any:
    return await org.c.post(
        f"{API}/project-roles/{role_id}/assignments",
        json={"placement_id": str(placement_id), "allocation_pct": pct, "start_date": start, **extra},
        headers=org.h(),
    )


async def deliverable(org: COrg, project_id: str, **body: Any) -> dict[str, Any]:
    payload = {"name": "Design document", "acceptance_criteria": "Signed off by the CTO", **body}
    return dict(
        await ok(await org.c.post(f"{API}/projects/{project_id}/deliverables", json=payload, headers=org.h()), 201)
    )


async def submitted(org: COrg, deliverable_id: str) -> dict[str, Any]:
    await ok(await org.c.post(f"{API}/deliverables/{deliverable_id}/start", headers=org.h()))
    return dict(await ok(await org.c.post(f"{API}/deliverables/{deliverable_id}/submit", headers=org.h())))


async def client_user(
    app: FastAPI, org: COrg, *, client_id: uuid.UUID | None = None, perms: tuple[str, ...] = PORTAL_PERMS, **kw: Any
) -> External:
    cid = client_id or org.client_id
    cu = await make_client_user(org.tenant_id, cid, permissions=perms, **kw)
    return await sign_in_external(app, "client_user", cu, org.tenant_id, cid)
