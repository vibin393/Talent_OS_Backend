"""Shared helpers for the Epic 24 bench tests."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.events import emit
from app.events.placements import PlacementActivated, PlacementEnded
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import Member, join
from tests.modules.placements.helpers import direct_placement

BENCH = "/api/v1/bench"
ROLES = "/api/v1/admin/roles"


async def placement(
    org: DOrg,
    *,
    candidate_id: uuid.UUID | None = None,
    status: str = "active",
    worker_type: str = "employee",
    start: date | None = None,
    end: date | None = None,
    planned_end: date | None = None,
    **body: Any,
) -> dict[str, Any]:
    """Create a direct placement forced into ``status`` (tests set the columns directly)."""
    start = start or date.today() - timedelta(days=60)
    p = await direct_placement(org, candidate_id, planned_start_date=start.isoformat(), **body)
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE placements SET status = :s, worker_type = :w, actual_start_date = :a, end_date = :e, "
                "planned_end_date = :pe WHERE id = :i"
            ),
            {
                "s": status,
                "w": worker_type,
                "a": start if status in ("active", "ended") else None,
                "e": end,
                "pe": planned_end,
                "i": uuid.UUID(p["id"]),
            },
        )
    return p


async def end_placement(
    org: DOrg, p: dict[str, Any], end: date, *, eligible: bool = True, worker_type: str = "employee"
) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE placements SET status = 'ended', end_date = :e WHERE id = :i"),
            {"e": end, "i": uuid.UUID(p["id"])},
        )
        emit(
            db,
            PlacementEnded(
                tenant_id=org.tenant_id,
                placement_id=uuid.UUID(p["id"]),
                candidate_id=uuid.UUID(p["candidate_id"]),
                end_date=end,
                reason="contract_completed",
                worker_type=worker_type,
                bench_eligible=eligible,
            ),
        )


async def activate(org: DOrg, p: dict[str, Any]) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE placements SET status = 'active', actual_start_date = :d WHERE id = :i"),
            {"d": date.today(), "i": uuid.UUID(p["id"])},
        )
        emit(
            db,
            PlacementActivated(
                tenant_id=org.tenant_id,
                placement_id=uuid.UUID(p["id"]),
                candidate_id=uuid.UUID(p["candidate_id"]),
                actual_start_date=date.today(),
            ),
        )


async def entries(org: DOrg, candidate_id: uuid.UUID | str) -> list[dict[str, Any]]:
    async with tenant_session(org.tenant_id) as db:
        rows = await db.execute(
            text("SELECT * FROM bench_entries WHERE candidate_id = :c ORDER BY created_at"),
            {"c": uuid.UUID(str(candidate_id))},
        )
        return [dict(r._mapping) for r in rows]


async def put_match(
    org: DOrg, demand_id: uuid.UUID | str, candidate_id: uuid.UUID | str, overall: str
) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO matches (id, tenant_id, demand_id, candidate_id, overall, sub_scores, "
                "scoring_version, input_hash, computed_at) "
                "VALUES (:id, :t, :d, :c, :o, '{\"skills\": 80}'::jsonb, 'v1', 'h', :now) "
                "ON CONFLICT (demand_id, candidate_id) DO UPDATE SET overall = EXCLUDED.overall"
            ),
            {
                "id": uuid.uuid4(),
                "t": org.tenant_id,
                "d": uuid.UUID(str(demand_id)),
                "c": uuid.UUID(str(candidate_id)),
                "o": Decimal(overall),
                "now": datetime.now(UTC),
            },
        )


async def grant_consent(org: DOrg, candidate_id: uuid.UUID | str) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE candidates SET ai_consent_state = 'granted' WHERE id = :c"),
            {"c": uuid.UUID(str(candidate_id))},
        )


async def role_member(app: FastAPI, org: DOrg, key: str, permissions: dict[str, str], name: str) -> Member:
    resp = await org.admin.client.post(
        ROLES,
        json={"key": key, "name": key.replace("_", " ").title(), "permissions": permissions},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return await join(app, org.tenant, key, name=name)
