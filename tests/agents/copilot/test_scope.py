# ruff: noqa: E501
"""Epic 54 scope suite (54.2, NFR-46): the Copilot read tools run with Effective Rights (user ∩ Copilot ceiling).
Out-of-scope and other-tenant records behave exactly like missing ones; masked fields stay masked; list totals
count only visible records. Tools are called the way the executor calls them (agent principal, owner context)."""

import dataclasses
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.agentic import get_agent, get_tool
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import tenant_session
from app.core.errors import Forbidden, NotFound
from app.core.permissions import Principal
from app.services import entity_refs
from app.services.agent_runtime.principals import intersect
from app.services.agent_runtime.state import ToolCtx
from tests.agents.copilot.conftest import CopilotOrg
from tests.modules.demands.helpers import open_demand
from tests.modules.identity.conftest import join


async def call(org: CopilotOrg, user_id: uuid.UUID, key: str, args: dict[str, Any]) -> Any:
    spec = get_agent("copilot")
    tool = get_tool(key)
    async with tenant_session(org.tenant_id) as db:
        owner = await entity_refs.principal_for_user(db, org.tenant_id, user_id)
        assert owner is not None
        principal = Principal(
            type="agent", id=uuid.uuid4(), tenant_id=org.tenant_id, permissions=intersect(owner.permissions, spec.permission_ceiling),
            team_ids=owner.team_ids, business_unit_id=owner.business_unit_id,
        )  # fmt: skip
        ctx = RequestContext(
            tenant_id=org.tenant_id,
            principal=principal,
            actor_kind="agent",
            actor_id=principal.id,
            on_behalf_of=user_id,
            agent_key="copilot",
        )
        tctx = ToolCtx(
            request=ctx,
            db=db,
            actor=None,
            run_id=None,
            agent_key="copilot",
            owner_user_id=user_id,
            subject=None,
            idempotency_key="t",
            execution="agent",
        )  # type: ignore[arg-type]
        token = set_context(ctx)
        try:
            out = await tool.handler(tctx, tool.input_model.model_validate(args))
        finally:
            reset_context(token)
        return out


@dataclasses.dataclass
class Scoped:
    org: CopilotOrg
    recruiter: Any
    demand: dict[str, Any]


@pytest.fixture
async def scoped(cop: CopilotOrg, app: FastAPI) -> Any:
    recruiter = await join(app, cop.tenant, "recruiter", name="Rita Recruiter")
    demand = await open_demand(cop.admin, cop.client_id, title="Senior Java Engineer")
    yield Scoped(cop, recruiter, demand)
    await recruiter.client.aclose()


async def test_invisible_demand_is_indistinguishable_from_missing(scoped: Scoped) -> None:
    org, rid = scoped.org, scoped.recruiter.user_id
    req = scoped.demand["req_id"]
    admin_view = await call(org, org.admin.user_id, "demands.list", {"q": req})
    assert admin_view.data.total == 1
    hidden = await call(org, rid, "demands.list", {"q": req})
    missing = await call(org, rid, "demands.list", {"q": "REQ-999999"})
    assert hidden.data.model_dump() == missing.data.model_dump() == {"total": 0, "rows": []}
    assert hidden.summary == missing.summary
    for key, args in (
        ("demands.get", {"demand_id": scoped.demand["id"]}),
        ("matching.rank", {"demand_id": scoped.demand["id"]}),
        ("pipeline.list", {"demand_id": scoped.demand["id"]}),
    ):
        with pytest.raises((NotFound, Forbidden)) as hidden_exc:
            await call(org, rid, key, args)
        with pytest.raises((NotFound, Forbidden)) as missing_exc:
            await call(org, rid, key, {**args, "demand_id": str(uuid.uuid4())})
        assert type(hidden_exc.value) is type(missing_exc.value) or isinstance(hidden_exc.value, NotFound), (
            key
        )
    at_risk = await call(org, rid, "sla.at_risk", {})
    assert all(r.demand_id != uuid.UUID(scoped.demand["id"]) for r in at_risk.data.rows)


async def test_admin_reads_demand_and_refs(scoped: Scoped) -> None:
    org = scoped.org
    out = await call(org, org.admin.user_id, "demands.get", {"demand_id": scoped.demand["id"]})
    assert out.data.human_id == scoped.demand["req_id"] and out.data.url == f"/demands/{scoped.demand['id']}"
    assert ("demand", uuid.UUID(scoped.demand["id"])) in out.refs


async def test_compensation_masked_without_permission(scoped: Scoped) -> None:
    org = scoped.org
    resp = await org.admin.client.patch(
        f"/api/v1/candidates/{org.candidate_id}", json={"expected_ctc_amount": "2500000", "expected_ctc_currency": "INR", "expected_ctc_unit": "annual"},
        headers={"X-CSRF-Token": org.admin.client.cookies.get("tos_csrf") or ""},
    )  # fmt: skip
    hidden = await call(
        org, scoped.recruiter.user_id, "candidates.get", {"candidate_id": str(org.candidate_id)}
    )
    assert hidden.data.expected_comp is None
    if resp.status_code < 300:
        assert hidden.data.compensation_hidden is True
        shown = await call(org, org.admin.user_id, "candidates.get", {"candidate_id": str(org.candidate_id)})
        assert shown.data.expected_comp is not None and shown.data.compensation_hidden is False
    demand = await call(org, scoped.org.admin.user_id, "demands.get", {"demand_id": scoped.demand["id"]})
    assert demand.data.bill_rate is not None and demand.data.rates_hidden is False


async def test_cross_tenant_is_not_found(cop: CopilotOrg, other_cop: CopilotOrg) -> None:
    for key, args in (
        ("candidates.get", {"candidate_id": str(other_cop.candidate_id)}),
        ("clients.get", {"client_id": str(other_cop.client_id)}),
        ("activities.list", {"entity_type": "candidate", "entity_id": str(other_cop.candidate_id)}),
        ("pipeline.list", {"candidate_id": str(other_cop.candidate_id)}),
    ):
        with pytest.raises(NotFound):
            await call(cop, cop.admin.user_id, key, args)
    out = await call(cop, cop.admin.user_id, "search.global", {"q": "Priya"})
    assert all(h.id != str(other_cop.candidate_id) for h in out.data.hits)


async def test_navigation_links_only_visible_records(scoped: Scoped) -> None:
    org = scoped.org
    seen = await call(
        org,
        org.admin.user_id,
        "navigation.screen_for",
        {"action": "reject_application", "entity_type": "demand", "entity_id": scoped.demand["id"]},
    )
    assert seen.data.url == f"/demands/{scoped.demand['id']}" and "person" in seen.data.reason.lower()
    hidden = await call(
        org,
        scoped.recruiter.user_id,
        "navigation.screen_for",
        {"action": "reject_application", "entity_type": "demand", "entity_id": scoped.demand["id"]},
    )
    assert hidden.data.url == "/demands" and hidden.refs == ()


async def test_every_read_tool_runs_for_the_owner(cop: CopilotOrg) -> None:
    """Smoke: each read tool executes under Effective Rights without error on seed data."""
    cid = str(cop.candidate_id)
    cases: list[tuple[str, dict[str, Any]]] = [
        ("search.global", {"q": "Priya"}), ("candidates.search", {"keywords": "Priya"}), ("candidates.get", {"candidate_id": cid}),
        ("demands.list", {}), ("sla.at_risk", {}), ("pipeline.list", {"candidate_id": cid}), ("interviews.list", {"mine": True}),
        ("placements.list", {}), ("tasks.list", {}), ("activities.list", {"entity_type": "candidate", "entity_id": cid}),
        ("clients.get", {"client_id": str(cop.client_id)}), ("navigation.screen_for", {"action": "export"}),
    ]  # fmt: skip
    for key, args in cases:
        out = await call(cop, cop.admin.user_id, key, args)
        assert out.summary, key
        get_tool(key).output_model.model_validate(out.data.model_dump())
