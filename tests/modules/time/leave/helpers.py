"""Shared helpers for the Epic 26 leave tests."""

import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI

from tests.modules.demands.helpers import DOrg, h
from tests.modules.time.helpers import AM, TIME, Setup, active

LEAVE = "/api/v1/portal/work/leave-requests"
WORK = "/api/v1/portal/work"
APPROVALS = "/api/v1/approvals"
CSRF = {"X-CSRF-Token": "csrf-token"}


def csrf() -> dict[str, str]:
    from app.core.security import CSRF_HEADER

    return {CSRF_HEADER: "csrf-token"}


async def leave_type(org: DOrg, **body: Any) -> dict[str, Any]:
    payload = {
        "code": f"EL{uuid.uuid4().hex[:4]}",
        "name": "Earned Leave",
        "accrual_mode": "monthly",
        "accrual_amount": "1.75",
    }
    payload.update(body)
    resp = await org.admin.client.post(f"{TIME}/leave-types", json=payload, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def legal_entity_of(org: DOrg, s: Setup) -> str:
    return str(s.placement["legal_entity_id"])


async def grant(org: DOrg, s: Setup, type_id: str, days: str, *, year: str | None = None) -> dict[str, Any]:
    resp = await org.admin.client.post(
        f"{TIME}/leave-balances/adjust",
        json={
            "candidate_id": str(s.candidate_id),
            "leave_type_id": type_id,
            "legal_entity_id": await legal_entity_of(org, s),
            "days": days,
            "reason": "Opening grant",
            "leave_year": year or str(s.start.year),
        },
        headers=h(org.admin),
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def balance(org: DOrg, s: Setup, type_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(
        f"{TIME}/leave-balances",
        params={"candidate_id": str(s.candidate_id), "leave_year": str(s.start.year)},
    )
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["leave_type_id"] == type_id]
    return dict(rows[0]) if rows else {}


async def request(s: Setup, type_id: str, start: date, end: date, **body: Any) -> Any:
    payload = {"leave_type_id": type_id, "start_date": start.isoformat(), "end_date": end.isoformat(), **body}
    return await s.worker.post(LEAVE, json=payload, headers=csrf())


async def internal(org: DOrg, request_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(f"{TIME}/leave-requests/{request_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def decide(org: DOrg, request_id: str, action: str = "approve", comment: str | None = None) -> Any:
    d = await internal(org, request_id)
    return await org.admin.client.post(
        f"{APPROVALS}/{d['approval_request_id']}/decide",
        json={"action": action, "comment": comment},
        headers=h(org.admin),
    )


async def setup(app: FastAPI, org: DOrg, **kw: Any) -> Setup:
    return await active(app, org, route=[AM], **kw)


def friday_of(s: Setup) -> date:
    return s.start + timedelta(days=4)
