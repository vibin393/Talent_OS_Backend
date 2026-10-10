# ruff: noqa: D401, E501
"""Shared helpers for the Epic 41 internal mobility tests."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import tenant_session
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.identity.conftest import Member, join
from tests.modules.placements.helpers import PLACEMENTS, direct_placement, waive_all
from tests.p2 import sign_in_candidate

API = "/api/v1"
M = f"{API}/mobility"
WORK = f"{API}/portal/work/opportunities"
APPROVALS = f"{API}/approvals"
TODAY = date.today()
ACK = {"disclosure_ack": True, "cover_note": "I would love to move to this team."}


async def demand(org: DOrg, visibility: str = "internal", **kw: Any) -> dict[str, Any]:
    return await open_demand(org.admin, org.client_id, visibility=visibility, **kw)


async def execute(org: DOrg, stmt: str, **params: Any) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(text(stmt), params)


async def rows(org: DOrg, stmt: str, **params: Any) -> list[Any]:
    async with tenant_session(org.tenant_id) as db:
        return list((await db.execute(text(stmt), params)).all())


async def scalar(org: DOrg, stmt: str, **params: Any) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(text(stmt), params)).scalar()


async def active_worker(
    org: DOrg, *, months: int = 24, account_manager: uuid.UUID | None = None, location: str | None = "Chennai"
) -> tuple[dict[str, Any], uuid.UUID]:
    """A Worker on an active Placement that started ``months`` ago (Account Manager = ``account_manager``)."""
    p = await direct_placement(org, planned_start_date=(TODAY - timedelta(days=1)).isoformat())
    await waive_all(org, p["id"])
    r = await org.admin.client.post(
        f"{PLACEMENTS}/{p['id']}/join", json={"actual_start_date": (TODAY - timedelta(days=1)).isoformat()}, headers=h(org.admin)
    )
    assert r.status_code == 200, r.text
    start = TODAY - timedelta(days=int(months * 30.5) + 1)
    await execute(
        org,
        "UPDATE placements SET actual_start_date = :s, planned_start_date = :s, account_manager_user_id = :am, "
        "work_location_text = :loc WHERE id = :i",
        s=start,
        am=account_manager or org.admin.user_id,
        loc=location,
        i=uuid.UUID(p["id"]),
    )
    return p, uuid.UUID(p["candidate_id"])


async def worker(app: FastAPI, org: DOrg, cid: uuid.UUID) -> Any:
    return await sign_in_candidate(app, cid, org.tenant_id)


async def employee(app: FastAPI, org: DOrg, name: str = "Esha Employee", *roles: str) -> Member:
    return await join(app, org.tenant, *(roles or ("viewer",)), name=name)


async def team_with_lead(org: DOrg, lead: uuid.UUID, *members: uuid.UUID) -> uuid.UUID:
    tid = uuid.uuid4()
    await execute(org, "INSERT INTO teams (id, tenant_id, name) VALUES (:i, :t, :n)", i=tid, t=org.tenant_id, n=f"Team {tid.hex[:6]}")
    await execute(org, "INSERT INTO team_members (tenant_id, team_id, user_id, is_lead) VALUES (:t, :team, :u, true)", t=org.tenant_id, team=tid, u=lead)
    for m in members:
        await execute(org, "INSERT INTO team_members (tenant_id, team_id, user_id, is_lead) VALUES (:t, :team, :u, false)", t=org.tenant_id, team=tid, u=m)
    return tid


async def overlay(org: DOrg, mobility_application_id: str) -> Any:
    return (
        await rows(
            org,
            "SELECT status, approval_request_id, manager_user_id, rejection_reason, application_id, candidate_id "
            "FROM mobility_applications WHERE id = :i",
            i=uuid.UUID(mobility_application_id),
        )
    )[0]


async def decide(m: Member, request_id: Any, action: str, comment: str | None = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if comment is not None:
        body["comment"] = comment
    return await m.client.post(f"{APPROVALS}/{request_id}/decide", json=body, headers=h(m))


async def stage_key(org: DOrg, application_id: Any) -> str:
    return str(
        await scalar(
            org,
            "SELECT s.key FROM applications a JOIN pipeline_stages s ON s.id = a.stage_id WHERE a.id = :i",
            i=uuid.UUID(str(application_id)),
        )
    )


async def set_stage(org: DOrg, application_id: Any, key: str) -> None:
    await execute(
        org,
        "UPDATE applications SET stage_id = (SELECT id FROM pipeline_stages WHERE key = :k) WHERE id = :i",
        k=key,
        i=uuid.UUID(str(application_id)),
    )


async def store_match(org: DOrg, demand_id: Any, candidate_id: Any, overall: str = "82.0", **kw: Any) -> None:
    """A stored ``matches`` row (as a scoring run would leave it)."""
    from app.models.matching import Match

    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("DELETE FROM matches WHERE demand_id = :d AND candidate_id = :c"),
            {"d": uuid.UUID(str(demand_id)), "c": uuid.UUID(str(candidate_id))},
        )
        db.add(
            Match(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                demand_id=uuid.UUID(str(demand_id)),
                candidate_id=uuid.UUID(str(candidate_id)),
                overall=Decimal(overall),
                sub_scores=kw.get("sub_scores", {"skills": 90, "compensation": 40}),
                evidence=kw.get(
                    "evidence",
                    [
                        {"id": "skill:pyspark", "kind": "skill", "text": "4 yrs PySpark", "comp": False},
                        {"id": "compensation", "kind": "compensation", "text": "Expects 30 LPA", "comp": True},
                        {"id": "location", "kind": "location", "text": "Based in Chennai", "comp": False},
                    ],
                ),
                missing_mandatory=kw.get("missing", ["Azure"]),
                scoring_version="test",
                input_hash="x" * 64,
                computed_at=datetime.now(UTC),
            )
        )


async def apply_user(member: Member, demand_id: Any, body: dict[str, Any] | None = None) -> Any:
    return await member.client.post(f"{M}/openings/{demand_id}/apply", json=body or ACK, headers=member.h())


async def apply_worker(client: Any, demand_id: Any, body: dict[str, Any] | None = None) -> Any:
    return await client.post(f"{WORK}/{demand_id}/apply", json=body or ACK, headers={"X-CSRF-Token": "csrf-token"})
