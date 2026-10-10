"""Epic 38 test helpers (segments, talent pools, rediscovery)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.matching import Match
from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage
from app.models.talent_pools import TalentPool, TalentPoolMember, TalentPoolRefresh
from tests.epic5 import h, make_candidate
from tests.modules.identity.conftest import Member

POOLS = "/api/v1/talent-pools"
SEGMENTS = "/api/v1/segments"
LISTS = "/api/v1/candidate-lists"

NOTICE_LTE_30 = {"filters": {"field": "notice_period_days", "op": "lte", "value": 30}}


def ctx_for(
    tenant_id: uuid.UUID, user_id: uuid.UUID, *, candidate_scope: Scope = Scope.TENANT, **extra: Scope
) -> RequestContext:
    perms = dict.fromkeys(CATALOG, Scope.TENANT)
    perms["candidate:read"] = candidate_scope
    perms.update(extra)
    principal = Principal(type="user", id=user_id, tenant_id=tenant_id, permissions=perms)
    return RequestContext(tenant_id=tenant_id, principal=principal)


async def candidate(m: Member, name: str, **body: Any) -> uuid.UUID:
    return uuid.UUID((await make_candidate(m, name, **body))["id"])


async def create_pool(m: Member, **body: Any) -> dict[str, Any]:
    resp = await m.client.post(POOLS, json={"name": "Pool", **body}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def dynamic_pool(m: Member, definition: dict[str, Any] | None = None, **body: Any) -> dict[str, Any]:
    return await create_pool(
        m,
        kind="dynamic",
        segment={"source_key": "candidates.candidate", "definition": definition or NOTICE_LTE_30},
        **body,
    )


async def active_member_ids(tenant_id: uuid.UUID, pool_id: Any) -> set[uuid.UUID]:
    async with tenant_session(tenant_id) as db:
        rows = await db.execute(
            select(TalentPoolMember.candidate_id).where(
                TalentPoolMember.pool_id == uuid.UUID(str(pool_id)), TalentPoolMember.removed_at.is_(None)
            )
        )
        return set(rows.scalars())


async def refreshes(tenant_id: uuid.UUID, pool_id: Any) -> list[TalentPoolRefresh]:
    async with tenant_session(tenant_id) as db:
        rows = await db.execute(
            select(TalentPoolRefresh)
            .where(TalentPoolRefresh.pool_id == uuid.UUID(str(pool_id)))
            .order_by(TalentPoolRefresh.started_at)
        )
        return list(rows.scalars())


async def pool_row(tenant_id: uuid.UUID, pool_id: Any) -> TalentPool:
    async with tenant_session(tenant_id) as db:
        pool = await db.get(TalentPool, uuid.UUID(str(pool_id)))
        assert pool is not None
        return pool


async def add_match(tenant_id: uuid.UUID, demand_id: Any, candidate_id: uuid.UUID, overall: str) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(
            delete(Match).where(Match.demand_id == uuid.UUID(str(demand_id)), Match.candidate_id == candidate_id)
        )
        db.add(
            Match(
                tenant_id=tenant_id,
                demand_id=uuid.UUID(str(demand_id)),
                candidate_id=candidate_id,
                overall=Decimal(overall),
                sub_scores={},
                scoring_version="test",
                input_hash="x" * 64,
                computed_at=datetime.now(UTC),
            )
        )


async def reached_stage(
    tenant_id: uuid.UUID, application_id: uuid.UUID, stage_key: str, days_ago: int
) -> None:
    async with tenant_session(tenant_id) as db:
        stage = (await db.execute(select(PipelineStage).where(PipelineStage.key == stage_key))).scalar_one()
        app = await db.get(Application, application_id)
        assert app is not None
        db.add(
            ApplicationStageHistory(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                application_id=application_id,
                from_stage_id=None,
                to_stage_id=stage.id,
                actor_kind="system",
                actor_id=None,
                reason_code=None,
                note=None,
                at=datetime.now(UTC) - timedelta(days=days_ago),
            )
        )
