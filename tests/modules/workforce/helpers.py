# ruff: noqa: D401
"""Shared helpers of the Epic 44 (workforce planning) tests."""

import uuid
from typing import Any

from app.core.database import tenant_session
from app.core.events import emit
from app.events.placements import PlacementActivated
from app.models.placements import Placement
from tests.modules.commercials.helpers import API, COrg, err, ok
from tests.modules.demands.helpers import ready_body

WF = f"{API}/workforce"
CP = f"{API}/client-portal"

__all__ = ["API", "CP", "WF", "COrg", "err", "ok"]


async def skill(org: COrg, name: str = "Java") -> str:
    """The catalogue skill named ``name`` (platform seed or a new tenant skill) as a ``SkillKey`` string."""
    resp = await org.c.post(f"{API}/skills", json={"name": name}, headers=org.h())
    if resp.status_code == 409:
        found = await ok(await org.c.get(f"{API}/skills", params={"q": name}, headers=org.h()))
        items = found["items"] if isinstance(found, dict) else found
        out = next(s for s in items if s["name"].lower() == name.lower())
    else:
        out = await ok(resp, 201)
    return f"{out['kind']}:{out['id']}"


async def plan(org: COrg, **body: Any) -> dict[str, Any]:
    payload = {"name": "FY 27-28 plan", "period_start": "2027-04-01", "period_end": "2028-03-31", **body}
    return dict(await ok(await org.c.post(f"{WF}/plans", json=payload, headers=org.h()), 201))


def line_body(**body: Any) -> dict[str, Any]:
    return {
        "role_title": "Java Developer",
        "headcount": 2,
        "start_month": "2027-05",
        "budget_amount": "2400000.00",
        "line_type": "new",
        **body,
    }


async def add_line(org: COrg, plan_id: str, **body: Any) -> dict[str, Any]:
    return dict(
        await ok(
            await org.c.post(f"{WF}/plans/{plan_id}/lines", json=line_body(**body), headers=org.h()), 201
        )
    )


async def act(org: COrg, plan_id: str, action: str, code: int = 200, **body: Any) -> Any:
    resp = await org.c.post(f"{WF}/plans/{plan_id}/{action}", json=body or None, headers=org.h())
    return await ok(resp, code)


async def decide(org: COrg, request_id: str, action: str = "approve", comment: str | None = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if comment:
        body["comment"] = comment
    return await ok(await org.c.post(f"{API}/approvals/{request_id}/decide", json=body, headers=org.h()))


async def approved_plan(
    org: COrg, *, lines: list[dict[str, Any]] | None = None, **plan_body: Any
) -> dict[str, Any]:
    """Create -> lines -> budget -> submit -> approve (the admin is a Tenant Admin, the default approver)."""
    p = await plan(org, **plan_body)
    for body in lines or [{}]:
        await add_line(org, p["id"], **body)
    await act(org, p["id"], "budget")
    submitted = await act(org, p["id"], "submit")
    assert submitted["status"] == "pending_approval", submitted
    await decide(org, submitted["approval_request_id"])
    out = await ok(await org.c.get(f"{WF}/plans/{p['id']}", headers=org.h()))
    assert out["status"] == "approved", out
    return dict(out)


async def slots(org: COrg, plan_id: str, status: str | None = None) -> list[dict[str, Any]]:
    url = f"{WF}/plans/{plan_id}/slots" + (f"?status={status}" if status else "")
    return list(await ok(await org.c.get(url, headers=org.h())))


async def open_demand(org: COrg, demand_id: str) -> dict[str, Any]:
    body = ready_body(org.client_id)
    for key in ("title", "client_id", "positions"):
        body.pop(key)
    await ok(await org.c.patch(f"{API}/demands/{demand_id}", json=body, headers=org.h()))
    out = await ok(await org.c.post(f"{API}/demands/{demand_id}/submit", headers=org.h()))
    assert out["status"] == "open", out
    return dict(out)


async def activate_placement(org: COrg, demand_id: uuid.UUID, cand: uuid.UUID, **extra: Any) -> uuid.UUID:
    """A Placement on ``demand_id`` and its ``PlacementActivated`` event (dispatched after commit)."""
    from datetime import date

    pid = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(
            Placement(
                id=pid,
                tenant_id=org.tenant_id,
                human_id=f"PLC-{uuid.uuid4().hex[:6]}",
                candidate_id=cand,
                client_id=org.client_id,
                legal_entity_id=org.entity_id,
                demand_id=demand_id,
                worker_type="employee",
                payee_kind="worker",
                planned_start_date=extra.pop("start", date(2027, 5, 1)),
                actual_start_date=date(2027, 5, 1),
                currency="INR",
                status="active",
                **extra,
            )
        )
        await db.flush()
        emit(
            db,
            PlacementActivated(
                tenant_id=org.tenant_id,
                placement_id=pid,
                candidate_id=cand,
                actual_start_date=date(2027, 5, 1),
            ),
        )
    return pid


async def candidate(org: COrg, name: str = "Priya Raman") -> uuid.UUID:
    from tests.epic3 import create_candidate

    return uuid.UUID((await create_candidate(org.admin, full_name=name))["id"])
