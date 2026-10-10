"""FR-65: AI and workflow actors cannot change demand status, decide an approval step or publish."""

import uuid

import pytest

from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.models.demands import Demand
from app.schemas.approvals import DecideIn
from app.schemas.demands import PostingIn
from app.services import approvals
from app.services import demands as svc
from tests.modules.demands.helpers import DOrg, admin_ctx, create_demand, open_demand

ACTORS = ("ai", "workflow")


@pytest.mark.parametrize("actor", ACTORS)
async def test_cannot_transition(dorg: DOrg, actor: str) -> None:
    opened = await open_demand(dorg.admin, dorg.client_id)
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        demand = await db.get(Demand, uuid.UUID(opened["id"]))
        assert demand is not None
        for to, reason in (("on_hold", "other"), ("closed", "other"), ("filled", None)):
            with pytest.raises(Forbidden) as exc:
                await svc.transition_demand(ctx, db, demand, to, actor, reason_code=reason)
            assert exc.value.code == "human_only_action"
        with pytest.raises(Forbidden):
            await svc.lifecycle.request_transition(ctx, db, demand.id, "on_hold", "other", actor_kind=actor)  # type: ignore[arg-type]
        assert demand.status == "open"


@pytest.mark.parametrize("actor", ACTORS)
async def test_cannot_submit(dorg: DOrg, actor: str) -> None:
    draft = await create_demand(dorg.admin, dorg.client_id)
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await svc.lifecycle.submit_demand(admin_ctx(dorg), db, uuid.UUID(draft["id"]), actor_kind=actor)  # type: ignore[arg-type]
        assert exc.value.code == "human_only_action"


@pytest.mark.parametrize("actor", ACTORS)
async def test_cannot_decide(dorg: DOrg, actor: str) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await approvals.decide(
                admin_ctx(dorg),
                db,
                uuid.uuid4(),
                DecideIn(action="approve"),
                actor_kind=actor,  # type: ignore[arg-type]
            )
        assert exc.value.code == "human_only_action"


@pytest.mark.parametrize("actor", ACTORS)
async def test_cannot_publish(dorg: DOrg, actor: str) -> None:
    opened = await open_demand(dorg.admin, dorg.client_id)
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await svc.jd.publish(
                admin_ctx(dorg),
                db,
                uuid.UUID(opened["id"]),
                PostingIn(jd_version_id=uuid.uuid4()),
                actor_kind=actor,  # type: ignore[arg-type]
            )
        assert exc.value.code == "human_only_action"
        with pytest.raises(Forbidden):
            await svc.jd.unpublish(admin_ctx(dorg), db, uuid.UUID(opened["id"]), actor_kind=actor)  # type: ignore[arg-type]
