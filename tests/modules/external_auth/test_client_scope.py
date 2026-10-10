"""AD-26 client scope (Epic 13, A4 + R13) and R7 system tasks."""

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app.core.client_scope import visible_demand_ids, visible_placement_ids
from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.errors import ValidationFailed
from app.core.permissions import Principal
from app.models.placements import Placement
from app.schemas.tasks import TaskCreate
from app.services import tasks_service
from tests.epic3 import create_candidate
from tests.modules.demands.helpers import DOrg, create_demand
from tests.modules.legal_entities.conftest import provision
from tests.p2 import make_client_user


def _ctx(org: DOrg, cu: uuid.UUID) -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(type="client_user", id=cu, tenant_id=org.tenant_id, org_id=org.client_id),
    )


async def test_visible_ids(dorg: DOrg) -> None:
    le = await provision(dorg.tenant)
    cand = uuid.UUID((await create_candidate(dorg.admin, full_name="Sam Scope"))["id"])
    demand = uuid.UUID((await create_demand(dorg.admin, dorg.client_id))["id"])
    p_direct, p_by_demand, p_hidden = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        for pid, _dem in ((p_direct, None), (p_by_demand, demand), (p_hidden, None)):
            db.add(
                Placement(
                    id=pid,
                    tenant_id=dorg.tenant_id,
                    human_id=f"PLC-{uuid.uuid4().hex[:6]}",
                    candidate_id=cand,
                    client_id=dorg.client_id,
                    legal_entity_id=le,
                    worker_type="employee",
                    payee_kind="worker",
                    planned_start_date=date(2026, 10, 1),
                    currency="INR",
                )
            )
        await db.flush()
        # link one placement to the scoped demand
        await db.execute(
            text("UPDATE placements SET demand_id = :d WHERE id = :i"), {"d": demand, "i": p_by_demand}
        )
    restricted = await make_client_user(dorg.tenant_id, dorg.client_id, restricted=True)
    open_user = await make_client_user(dorg.tenant_id, dorg.client_id, restricted=False)
    async with tenant_session(dorg.tenant_id) as db:
        for kind, sid in (("placement", p_direct), ("demand", demand)):
            await db.execute(
                text(
                    "INSERT INTO client_user_scopes (id, tenant_id, client_user_id, subject_kind, subject_id) "
                    "VALUES (:id, :t, :cu, :k, :s)"
                ),
                {"id": uuid.uuid4(), "t": dorg.tenant_id, "cu": restricted, "k": kind, "s": sid},
            )
    async with tenant_session(dorg.tenant_id, org=OrgScope.of("client", dorg.client_id)) as db:
        assert await visible_placement_ids(_ctx(dorg, restricted), db) == frozenset({p_direct, p_by_demand})
        assert await visible_demand_ids(_ctx(dorg, restricted), db) == frozenset({demand})
        assert await visible_placement_ids(_ctx(dorg, open_user), db) is None
        assert await visible_demand_ids(_ctx(dorg, open_user), db) is None
    async with tenant_session(dorg.tenant_id) as db:
        internal = RequestContext(
            tenant_id=dorg.tenant_id, principal=Principal(type="user", id=dorg.admin.user_id)
        )
        assert await visible_placement_ids(internal, db) is None


async def test_create_system_task(dorg: DOrg) -> None:
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    async with tenant_session(dorg.tenant_id) as db:
        task = await tasks_service.create_system_task(
            ctx, db, TaskCreate(title="Collect PAN", owner_id=dorg.admin.user_id), source="onboarding"
        )
        assert task.owner_id == dorg.admin.user_id
        row = (
            await db.execute(text("SELECT created_by, source FROM tasks WHERE id = :i"), {"i": task.id})
        ).one()
        assert row.created_by is None and row.source == "onboarding"
        with pytest.raises(ValidationFailed):
            await tasks_service.create_system_task(ctx, db, TaskCreate(title="No owner"), source="bgv")
        with pytest.raises(ValueError, match="unknown system task source"):
            await tasks_service.create_system_task(
                ctx, db, TaskCreate(title="x", owner_id=dorg.admin.user_id), source="nope"
            )
