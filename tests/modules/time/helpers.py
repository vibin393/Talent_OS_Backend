"""Shared helpers for the Epic 25 timesheet tests."""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI

from app.core.database import tenant_session
from app.core.security import CSRF_HEADER
from app.services import postings
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.placements.helpers import PLACEMENTS, direct_placement, waive_all
from tests.p2 import External, make_client_user, sign_in_candidate, sign_in_external

TIME = "/api/v1/time"
WORK = "/api/v1/portal/work/timesheets"
CLIENT = "/api/v1/client-portal/timesheets"
APPROVALS = "/api/v1/approvals"
CSRF = {CSRF_HEADER: "csrf-token"}

AM = {"kind": "placement_account_manager", "label": "Account Manager"}
CLIENT_ANY = {"kind": "client_any", "label": "Client"}
FINANCE = {"kind": "finance_role", "ref": "tenant_admin", "label": "Finance"}


def monday(weeks_ago: int) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_ago)


@dataclasses.dataclass
class Setup:
    org: DOrg
    placement: dict[str, Any]
    worker: httpx.AsyncClient
    start: date

    @property
    def pid(self) -> str:
        return str(self.placement["id"])

    @property
    def candidate_id(self) -> uuid.UUID:
        return uuid.UUID(self.placement["candidate_id"])


async def active(
    app: FastAPI,
    org: DOrg,
    *,
    weeks_ago: int = 2,
    route: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    **body: Any,
) -> Setup:
    start = monday(weeks_ago)
    p = await direct_placement(
        org, planned_start_date=start.isoformat(), account_manager_user_id=str(org.admin.user_id), **body
    )
    await waive_all(org, p["id"])
    resp = await org.admin.client.post(
        f"{PLACEMENTS}/{p['id']}/join", json={"actual_start_date": start.isoformat()}, headers=h(org.admin)
    )
    assert resp.status_code == 200, resp.text
    cfg = {"approval_route": route if route is not None else [AM, CLIENT_ANY, FINANCE], **(config or {})}
    resp = await org.admin.client.put(f"{TIME}/placements/{p['id']}/config", json=cfg, headers=h(org.admin))
    assert resp.status_code == 200, resp.text
    await org.admin.client.post(f"{TIME}/placements/{p['id']}/timesheets/generate", headers=h(org.admin))
    worker = await sign_in_candidate(app, uuid.UUID(p["candidate_id"]), org.tenant_id)
    return Setup(org, p, worker, start)


async def first_timesheet(s: Setup) -> dict[str, Any]:
    resp = await s.org.admin.client.get(f"{TIME}/timesheets", params={"placement_id": s.pid})
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["kind"] == "regular" and r["period_start"] == s.start.isoformat()]
    assert rows, resp.json()
    return dict(rows[0])


def week(start: date, *hours: str, category: str = "regular") -> list[dict[str, Any]]:
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "category": category, "hours": hv}
        for i, hv in enumerate(hours)
    ]


async def worker_save(s: Setup, ts_id: str, days: list[dict[str, Any]]) -> httpx.Response:
    return await s.worker.put(f"{WORK}/{ts_id}/entries", json={"days": days}, headers=CSRF)


async def worker_submit(s: Setup, ts_id: str, **body: Any) -> httpx.Response:
    return await s.worker.post(f"{WORK}/{ts_id}/submit", json=body, headers=CSRF)


async def detail(org: DOrg, ts_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(f"{TIME}/timesheets/{ts_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def admin_decide(
    org: DOrg, ts_id: str, action: str = "approve", comment: str | None = None
) -> httpx.Response:
    d = await detail(org, ts_id)
    return await org.admin.client.post(
        f"{APPROVALS}/{d['approval_request_id']}/decide",
        json={"action": action, "comment": comment},
        headers=h(org.admin),
    )


async def client_user(app: FastAPI, org: DOrg, **kw: Any) -> External:
    cu = await make_client_user(org.tenant_id, org.client_id, **kw)
    return await sign_in_external(app, "client_user", cu, org.tenant_id, org.client_id)


async def approve_all(org: DOrg, ts_id: str, client: External) -> dict[str, Any]:
    """Walk the route: internal steps via /approvals, Client steps via the Client Portal."""
    for _ in range(6):
        d = await detail(org, ts_id)
        if d["status"] == "approved":
            return d
        if d["current_step"] == "Client":
            resp = await client.client.post(
                f"{CLIENT}/{ts_id}/decide", json={"action": "approve"}, headers=client.h()
            )
        else:
            resp = await admin_decide(org, ts_id)
        assert resp.status_code == 200, resp.text
    return await detail(org, ts_id)


async def post_payable(org: DOrg, kind: str, source_ids: list[str], placement_id: str) -> list[uuid.UUID]:
    async with tenant_session(org.tenant_id) as db:
        return await postings.record_posted(
            admin_ctx(org),
            db,
            "payable",
            [
                postings.PostingLine(
                    kind,
                    uuid.UUID(sid),
                    "payroll_row",
                    uuid.uuid4(),
                    None,
                    Decimal("1"),
                    Decimal("1.00"),
                    "INR",
                    uuid.UUID(placement_id),
                    "worker",
                )
                for sid in source_ids
            ],
        )
