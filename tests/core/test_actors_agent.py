"""Epic 53 (53.2, AD-60): the ``agent`` actor kind is automated -- never human, never an approver."""

import uuid

import pytest

from app.core.actors import (
    APPROVER_KINDS,
    AUTOMATED_KINDS,
    HUMAN_KINDS,
    ActorRef,
    actor_from_ctx,
    is_human,
    require_human,
)
from app.core.context import RequestContext
from app.core.errors import Forbidden


def test_require_human_refuses_agent() -> None:
    actor = ActorRef("agent", uuid.uuid4())
    assert not is_human(actor)
    with pytest.raises(Forbidden) as info:
        require_human(actor)
    assert info.value.code == "human_only_action"
    assert info.value.status_code == 403


def test_actor_from_ctx_agent_carries_on_behalf_of() -> None:
    agent_id, owner_id = uuid.uuid4(), uuid.uuid4()
    ctx = RequestContext(tenant_id=uuid.uuid4(), actor_kind="agent", actor_id=agent_id, on_behalf_of=owner_id)
    assert actor_from_ctx(ctx) == ActorRef("agent", agent_id, on_behalf_of=owner_id)


def test_agent_kind_sets() -> None:
    assert "agent" in AUTOMATED_KINDS
    assert "agent" not in HUMAN_KINDS
    assert "agent" not in APPROVER_KINDS


async def test_approvals_decide_refuses_agent() -> None:
    """``approvals.decide`` refuses an agent actor with 403 ``human_only_action`` before touching the DB."""
    from app.services.approvals import service

    ctx = RequestContext(
        tenant_id=uuid.uuid4(), actor_kind="agent", actor_id=uuid.uuid4(), on_behalf_of=uuid.uuid4()
    )
    with pytest.raises(Forbidden) as info:
        await service.decide(ctx, None, uuid.uuid4(), None, actor=actor_from_ctx(ctx))  # type: ignore[arg-type]
    assert (info.value.code, info.value.status_code) == ("human_only_action", 403)


async def test_approvals_decide_derives_agent_from_ctx() -> None:
    from app.services.approvals import service

    ctx = RequestContext(tenant_id=uuid.uuid4(), actor_kind="agent", actor_id=uuid.uuid4())
    with pytest.raises(Forbidden) as info:
        await service.decide(ctx, None, uuid.uuid4(), None)  # type: ignore[arg-type]
    assert (info.value.code, info.value.status_code) == ("human_only_action", 403)
