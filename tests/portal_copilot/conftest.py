# ruff: noqa: E501
"""Epic 77 portal Copilot harness: one tenant with the agentic switch and the three portal Copilots on, Clients A / B
(each with a sentinel-named Demand; Client A has a second Demand), Client A users (unrestricted, restricted to Demand
A1, without ``portal_copilot:use``), a Client B user, Vendors X / Y (one Release each) with Vendor Users, Candidates
1 / 2 (each with an application). Model calls use the scripted Fake LLM only (``{"agent_copilot_<kind>": [...]}``)."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.agentic import get_agent
from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.permissions import Principal, Scope
from app.models.external_auth import ClientUserScope
from app.models.portal_copilot import PortalCopilotSettings
from app.models.vendors import VendorRelease
from app.services import ai
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.portal_copilot import ratelimit
from tests.epic3 import create_candidate
from tests.modules.demands.helpers import create_demand
from tests.modules.identity.conftest import Member, csrf, join, make_tenant
from tests.modules.offers.helpers import add_application
from tests.p2 import (
    External,
    make_client_user,
    make_vendor,
    make_vendor_user,
    sign_in_candidate,
    sign_in_external,
)

BASE = {"client": "/api/v1/client-portal/copilot", "vendor": "/api/v1/vendor-portal/copilot", "candidate": "/api/v1/portal/copilot"}
SENTINELS = ("Bravo Secret Role", "Yankee Hidden Release", "Second Candidate Role")
CLIENT_PERMS = ("portal_copilot:use", "submissions:review", "placements:view", "invoices:view", "time:approve")
VENDOR_PERMS = ("portal_copilot:use", "submissions:create", "releases:view", "placements:view", "statements:view")


@dataclasses.dataclass
class Who:
    kind: str
    id: uuid.UUID
    org_id: uuid.UUID
    client: httpx.AsyncClient
    csrf: dict[str, str]


@dataclasses.dataclass
class PortalOrg:
    tenant_id: uuid.UUID
    admin: Member
    client_a: uuid.UUID
    client_b: uuid.UUID
    demand_a1: uuid.UUID
    demand_a2: uuid.UUID
    demand_b1: uuid.UUID
    cu_a: Who
    cu_a_restricted: Who
    cu_a_nouse: Who
    cu_b: Who
    vendor_x: uuid.UUID
    vendor_y: uuid.UUID
    release_x: uuid.UUID
    release_y: uuid.UUID
    vu_x: Who
    vu_y: Who
    cand_1: Who
    cand_2: Who
    app_1: uuid.UUID
    app_2: uuid.UUID
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


async def _client(m: Member, name: str) -> uuid.UUID:
    resp = await m.client.post("/api/v1/clients", json={"name": name}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _who(kind: str, ext: External) -> Who:
    return Who(kind, ext.id, ext.org_id, ext.client, ext.h())


async def enable_all(tenant_id: uuid.UUID, **settings: Any) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(
            db, tenant_id, {"agentic": True, "agent_copilot_client": True, "agent_copilot_vendor": True, "agent_copilot_candidate": True}
        )
        db.add(
            PortalCopilotSettings(
                id=uuid.uuid4(), tenant_id=tenant_id, client_enabled=True, vendor_enabled=True, candidate_enabled=True, **settings
            )
        )


async def _release(tenant_id: uuid.UUID, vendor_id: uuid.UUID, demand_id: uuid.UUID, client_id: uuid.UUID, title: str) -> uuid.UUID:
    rid = uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        db.add(
            VendorRelease(
                id=rid, tenant_id=tenant_id, human_id=f"REL-{uuid.uuid4().hex[:6]}", demand_id=demand_id, vendor_id=vendor_id,
                client_id=client_id, status="active", released_at=datetime.now(UTC), title=title, positions=1, skills=["Python"],
                client_display_name="Confidential client", jd_public_text="Build data pipelines.",
            )
        )
    return rid


async def grant_candidate_consent(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
    from app.services import privacy

    async with tenant_session(tenant_id) as db:
        ctx = RequestContext(tenant_id=tenant_id, principal=Principal(type="candidate", id=candidate_id, tenant_id=tenant_id))
        await privacy.grant_consent(ctx, db, candidate_id, "ai_agent_communication", source="candidate_portal")


async def make_org(app: FastAPI) -> PortalOrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Ada Admin")
    tid = tenant.id
    await enable_all(tid)
    client_a = await _client(admin, "Alpha Industries")
    client_b = await _client(admin, "Bravo Holdings")
    d_a1 = uuid.UUID((await create_demand(admin, client_a, title="Alpha Data Engineer"))["id"])
    d_a2 = uuid.UUID((await create_demand(admin, client_a, title="Alpha Platform Lead"))["id"])
    d_b1 = uuid.UUID((await create_demand(admin, client_b, title=SENTINELS[0]))["id"])
    cu_a = await make_client_user(tid, client_a, permissions=CLIENT_PERMS)
    cu_r = await make_client_user(tid, client_a, permissions=CLIENT_PERMS, restricted=True)
    cu_n = await make_client_user(tid, client_a, permissions=("submissions:review",))
    cu_b = await make_client_user(tid, client_b, permissions=CLIENT_PERMS)
    async with tenant_session(tid) as db:
        db.add(ClientUserScope(id=uuid.uuid4(), tenant_id=tid, client_user_id=cu_r, subject_kind="demand", subject_id=d_a1))
    vx, vy = await make_vendor(tid), await make_vendor(tid)
    rx = await _release(tid, vx, d_a1, client_a, "Xray Open Release")
    ry = await _release(tid, vy, d_b1, client_b, SENTINELS[1])
    vu_x, vu_y = await make_vendor_user(tid, vx), await make_vendor_user(tid, vy)
    async with tenant_session(tid) as db:
        from sqlalchemy import update

        from app.models.external_auth import VendorUser

        await db.execute(update(VendorUser).where(VendorUser.id.in_([vu_x, vu_y])).values(portal_permissions=list(VENDOR_PERMS)))
    c1 = uuid.UUID((await create_candidate(admin, full_name="Cara One", emails=[{"email": f"c1-{uuid.uuid4().hex[:6]}@example.com"}]))["id"])
    c2 = uuid.UUID((await create_candidate(admin, full_name="Dev Two", emails=[{"email": f"c2-{uuid.uuid4().hex[:6]}@example.com"}]))["id"])
    d_c2 = uuid.UUID((await create_demand(admin, client_b, title=SENTINELS[2]))["id"])
    await _open(admin, d_a1)
    await _open(admin, d_a2)
    await _open(admin, d_c2)
    app1 = await add_application(admin, d_a1, c1)
    app2 = await add_application(admin, d_c2, c2)
    await grant_candidate_consent(tid, c1)
    await grant_candidate_consent(tid, c2)
    org = PortalOrg(
        tenant_id=tid, admin=admin, client_a=client_a, client_b=client_b, demand_a1=d_a1, demand_a2=d_a2, demand_b1=d_b1,
        cu_a=_who("client", await sign_in_external(app, "client_user", cu_a, tid, client_a)),
        cu_a_restricted=_who("client", await sign_in_external(app, "client_user", cu_r, tid, client_a)),
        cu_a_nouse=_who("client", await sign_in_external(app, "client_user", cu_n, tid, client_a)),
        cu_b=_who("client", await sign_in_external(app, "client_user", cu_b, tid, client_b)),
        vendor_x=vx, vendor_y=vy, release_x=rx, release_y=ry,
        vu_x=_who("vendor", await sign_in_external(app, "vendor_user", vu_x, tid, vx)),
        vu_y=_who("vendor", await sign_in_external(app, "vendor_user", vu_y, tid, vy)),
        cand_1=await _cand(app, c1, tid), cand_2=await _cand(app, c2, tid), app_1=app1, app_2=app2,
    )  # fmt: skip
    for who in (org.cu_a, org.cu_a_restricted, org.cu_b, org.vu_x, org.vu_y, org.cand_1, org.cand_2):
        await ratelimit.reset(tid, {"client": "client_user", "vendor": "vendor_user", "candidate": "candidate"}[who.kind], who.id)
    return org


async def _open(m: Member, demand_id: uuid.UUID) -> None:
    resp = await m.client.post(f"/api/v1/demands/{demand_id}/submit", headers=h(m))
    assert resp.status_code == 200, resp.text


async def _cand(app: FastAPI, candidate_id: uuid.UUID, tenant_id: uuid.UUID) -> Who:
    client = await sign_in_candidate(app, candidate_id, tenant_id)
    return Who("candidate", candidate_id, candidate_id, client, {"X-CSRF-Token": "csrf-token"})


@pytest.fixture
async def po(app: FastAPI) -> AsyncIterator[PortalOrg]:
    org = await make_org(app)
    yield org
    for who in (org.cu_a, org.cu_a_restricted, org.cu_a_nouse, org.cu_b, org.vu_x, org.vu_y, org.cand_1, org.cand_2):
        await who.client.aclose()
    await org.admin.client.aclose()


# ================================================================ HTTP helpers
def script(kind: str, *turns: dict[str, Any]) -> dict[str, list[FakeTurn]]:
    return {f"agent_copilot_{kind}": [FakeTurn.from_mapping(t) for t in turns]}


async def new_conversation(who: Who) -> dict[str, Any]:
    resp = await who.client.post(f"{BASE[who.kind]}/conversations", headers=who.csrf)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def ask(who: Who, conversation_id: str, text: str, turns: Sequence[dict[str, Any]] | None = None, *, expect: int = 200) -> tuple[dict[str, Any], Any]:
    with fake_llm(script(who.kind, *(turns or [{"kind": "text", "text": "OK."}]))) as fake:
        resp = await who.client.post(
            f"{BASE[who.kind]}/conversations/{conversation_id}/messages", json={"text": text},
            headers={**who.csrf, "Idempotency-Key": uuid.uuid4().hex},
        )  # fmt: skip
    assert resp.status_code == expect, resp.text
    return dict(resp.json()), fake


async def detail(who: Who, conversation_id: str) -> dict[str, Any]:
    resp = await who.client.get(f"{BASE[who.kind]}/conversations/{conversation_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def answer(conv: dict[str, Any]) -> dict[str, Any]:
    return next(m for m in reversed(conv["messages"]) if m["role"] == "assistant")


async def tool_calls(tenant_id: uuid.UUID, run_id: str) -> list[Any]:
    from sqlalchemy import select

    from app.models.agent_runtime import AgentToolCall

    async with tenant_session(tenant_id) as db:
        rows = list((await db.execute(select(AgentToolCall).where(AgentToolCall.run_id == uuid.UUID(run_id)).order_by(AgentToolCall.created_at))).scalars())
        for r in rows:
            db.expunge(r)
            r.tool_key = r.tool_key.replace("__", ".", 1)  # policy violations are logged under the wire name
        return rows


# ================================================================ direct tool calls (confirm-path context)
@dataclasses.dataclass
class _ToolCtx:
    request: RequestContext
    db: Any
    actor: Any
    run_id: uuid.UUID | None
    agent_key: str
    owner_user_id: uuid.UUID | None
    subject: Any
    idempotency_key: str
    execution: str


async def call_tool(org: PortalOrg, who: Who, tool_key: str, args: dict[str, Any]) -> Any:
    """Run one ``portal_*`` tool as the portal principal with its Effective Rights (ceiling ∩ portal permissions), on
    the principal's org-scoped session -- the same projection, scope and RLS path as a run."""
    from app.core.actors import ActorRef
    from app.core.agentic import get_tool
    from app.services.portal_copilot.principals import candidate_rights

    spec = get_agent(f"copilot_{who.kind}")
    tool = get_tool(tool_key)
    ptype = {"client": "client_user", "vendor": "vendor_user", "candidate": "candidate"}[who.kind]
    if who.kind == "candidate":
        perms = await candidate_rights(None, org.tenant_id, who.id)  # type: ignore[arg-type]
    else:
        from sqlalchemy import select

        from app.models.external_auth import ClientUser, VendorUser

        model = ClientUser if who.kind == "client" else VendorUser
        async with tenant_session(org.tenant_id) as db:
            perms = frozenset((await db.execute(select(model.portal_permissions).where(model.id == who.id))).scalar_one() or ())
    rights = {k: Scope.TENANT for k in spec.permission_ceiling if k in perms}
    principal = Principal(type=ptype, id=who.id, tenant_id=org.tenant_id, org_id=who.org_id, portal_permissions=frozenset(perms), permissions=rights)  # type: ignore[arg-type]
    missing = [p for p in tool.permissions if p not in rights]
    if missing:
        from app.core.errors import Forbidden

        raise Forbidden("no permission", details={"permission": missing[0]})
    req = RequestContext(tenant_id=org.tenant_id, principal=principal)
    org_kind = {"client": "client", "vendor": "vendor", "candidate": "worker"}[who.kind]
    async with tenant_session(org.tenant_id, org=OrgScope.of(org_kind, who.org_id)) as db:
        tctx = _ToolCtx(req, db, ActorRef(ptype, who.id), None, spec.key, None, None, "test", "copilot_confirm")  # type: ignore[arg-type]
        outcome = await tool.handler(tctx, tool.input_model.model_validate(args))
        return outcome.data.model_dump(mode="json")
