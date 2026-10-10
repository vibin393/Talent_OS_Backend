# ruff: noqa: E501
"""Epic 55: settings / sender-domain rules, cross-tenant 404s, API mutations are human-only, draft-only update rules,
★ guarantees, portal invisibility of agent drafts, P1 prompts pinned to v1, the connected-mailbox poll and the
180-day purge."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.agentic import get_agent, tools
from app.core.database import tenant_session
from app.models.agent_runtime import AgentToolCall
from app.models.ai import AIAction
from app.models.demand_intake import DemandIntakeMessage
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.demand.conftest import (
    DOrg,
    disable_triggers,
    eml,
    h,
    message_row,
    post_inbound,
    start_demand_run,
)

DEMANDS = "/api/v1/demands"


async def _demand(org: DOrg, **over: Any) -> dict[str, Any]:
    body = {"title": "Data Engineer", "client_id": str(org.client_id), "positions": 1, "country": "IN", "location": "Pune",
            "work_mode": "hybrid", "employment_type": "contract", "mandatory_skills": ["PySpark"], **over}  # fmt: skip
    resp = await org.admin.client.post(DEMANDS, json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_sender_domains_rules(dorg: DOrg) -> None:
    c = dorg.admin.client
    public = await c.post(
        f"{DEMANDS}/intake-sender-domains",
        json={"domain": "gmail.com", "client_id": str(dorg.client_id), "confirmed": True},
        headers=dorg.admin.h(),
    )
    assert public.status_code == 422 and public.json()["error"]["code"] == "public_domain_not_allowed"
    bad = await c.post(
        f"{DEMANDS}/intake-sender-domains",
        json={"domain": "not a domain", "client_id": str(dorg.client_id), "confirmed": True},
        headers=dorg.admin.h(),
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "domain_invalid"
    unconfirmed = await c.post(
        f"{DEMANDS}/intake-sender-domains",
        json={"domain": "acme.example", "client_id": str(dorg.client_id), "confirmed": False},
        headers=dorg.admin.h(),
    )
    assert unconfirmed.status_code == 422
    ok = await c.post(
        f"{DEMANDS}/intake-sender-domains",
        json={"domain": "@ACME.example", "client_id": str(dorg.client_id), "confirmed": True},
        headers=dorg.admin.h(),
    )
    assert ok.status_code == 201 and ok.json()["domain"] == "acme.example"
    listed = await c.get(f"{DEMANDS}/intake-sender-domains")
    assert [d["domain"] for d in listed.json()] == ["acme.example"]


async def test_verified_domain_and_ambiguous_matching(app: FastAPI, dorg: DOrg) -> None:
    from tests.agents.demand.conftest import create_client

    await disable_triggers(dorg, "intake_received", "draft_created")
    c = dorg.admin.client
    await c.post(
        f"{DEMANDS}/intake-sender-domains",
        json={"domain": "acme.example", "client_id": str(dorg.client_id), "confirmed": True},
        headers=dorg.admin.h(),
    )
    resp = await post_inbound(app, eml(dorg, sender="ravi@acme.example", body="Need a tester"), dorg.address)
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert (
        row.status == "matched" and row.match_method == "verified_domain" and row.matched_contact_id is None
    )
    other_client = await create_client(dorg.admin, "Acme Holdings")
    await c.post(
        f"{DEMANDS}/intake-sender-domains",
        json={"domain": "acme.example", "client_id": str(other_client), "confirmed": True},
        headers=dorg.admin.h(),
    )
    resp2 = await post_inbound(
        app, eml(dorg, sender="meera@acme.example", body="Need a QA lead"), dorg.address
    )
    row2 = await message_row(dorg, uuid.UUID(resp2.json()["message_id"]))
    assert row2.status == "ambiguous" and row2.task_id is not None


async def test_cross_tenant_reads_are_404(app: FastAPI, dorg: DOrg, dorg_other: DOrg) -> None:
    await disable_triggers(dorg, "intake_received", "draft_created")
    resp = await post_inbound(app, eml(dorg, sender="priya@acme.example", body="Need a tester"), dorg.address)
    mid = resp.json()["message_id"]
    d = await _demand(dorg)
    oc = dorg_other.admin.client
    assert (await oc.get(f"{DEMANDS}/intake-messages/{mid}")).status_code == 404
    assert (
        await oc.post(
            f"{DEMANDS}/intake-messages/{mid}/discard",
            json={"reason": "spam mail"},
            headers=dorg_other.admin.h(),
        )
    ).status_code == 404
    assert (await oc.get(f"{DEMANDS}/{d['id']}/insights")).status_code == 404
    listed = await oc.get(f"{DEMANDS}/intake-messages", params={"tab": "all"})
    assert listed.status_code == 200 and listed.json()["total"] == 0


async def test_inbox_tabs_and_discard(app: FastAPI, dorg: DOrg) -> None:
    await disable_triggers(dorg, "intake_received", "draft_created")
    await post_inbound(app, eml(dorg, sender="stranger@unknown.example", body="Need a tester"), dorg.address)
    c = dorg.admin.client
    attention = await c.get(f"{DEMANDS}/intake-messages", params={"tab": "needs_attention"})
    assert attention.json()["total"] == 1 and attention.json()["items"][0]["status"] == "unmatched"
    mid = attention.json()["items"][0]["id"]
    detail = await c.get(f"{DEMANDS}/intake-messages/{mid}")
    assert detail.status_code == 200 and detail.json()["match"]["status"] == "unmatched"
    gone = await c.post(
        f"{DEMANDS}/intake-messages/{mid}/discard",
        json={"reason": "not a requirement"},
        headers=dorg.admin.h(),
    )
    assert gone.status_code == 200 and gone.json()["status"] == "discarded"
    assert (await c.get(f"{DEMANDS}/intake-messages", params={"tab": "needs_attention"})).json()["total"] == 0


async def test_update_rules_on_draft_and_open(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    draft = await _demand(dorg)
    opened = await _demand(dorg, title="QA Engineer")
    resp = await dorg.admin.client.post(f"{DEMANDS}/{opened['id']}/submit", headers=h(dorg.admin))
    assert resp.status_code == 200 and resp.json()["status"] == "open", resp.text
    script = [
        FakeTurn(
            "tool_calls", calls=(("demands.update_draft", {"demand_id": opened["id"], "jd_content": "JD"}),)
        ),
        FakeTurn(
            "tool_calls",
            calls=(("demands.update_draft", {"demand_id": draft["id"], "fields": {"location": "Delhi"}}),),
        ),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "tried"}),
    ]
    with fake_llm({"agent_demand": script}):
        ref = await start_demand_run(dorg, "demand_opened", ("demand", uuid.UUID(opened["id"])))
    async with tenant_session(dorg.tenant_id) as db:
        calls = list(
            (
                await db.execute(
                    select(AgentToolCall)
                    .where(AgentToolCall.run_id == ref.run_id)
                    .order_by(AgentToolCall.step_no)
                )
            ).scalars()
        )
    first = calls[0]
    assert (
        first.tool_key == "demands.update_draft"
        and first.outcome == "failed"
        and first.error_code == "demand_not_draft"
    )
    # the draft is not the run's subject: not found (subject binding)
    assert calls[1].outcome == "not_found"
    # update on a draft -> 409 use_update_draft (executed directly as an approved Proposal would be)
    from app.core.errors import ConflictError, ValidationFailed
    from app.schemas.demand_insights import DemandUpdateIn, UpdateDraftIn
    from app.services import demand_insights_service as insights

    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await insights.apply_update(
                dorg.ctx(),
                db,
                DemandUpdateIn(demand_id=uuid.UUID(draft["id"]), jd_content="x"),
                execution="approved_proposal",
            )
        assert exc.value.code == "use_update_draft"
        # a field a person confirmed (not AI-extracted) cannot be changed by the agent
        with pytest.raises(ValidationFailed) as exc2:
            await insights.update_draft(
                dorg.ctx(),
                db,
                UpdateDraftIn(demand_id=uuid.UUID(draft["id"]), fields={"location": "Delhi"}),
                run_id=None,
            )  # type: ignore[arg-type]
        assert exc2.value.code == "field_confirmed_by_user"


def test_star_guarantees() -> None:
    from app.core.hooks import autodiscover

    autodiscover("app.services")
    mine = [t for t in tools().values() if t.owner_epic == "55"]
    assert len(mine) == 17 and all(t.transition is None for t in mine)
    spec = get_agent("demand")
    assert not spec.placeholder and spec.default_mode == "suggest"
    forbidden = {"demand:approve", "demand:assign", "identity:view", "settings:manage"}
    assert not forbidden & set(spec.permission_ceiling)
    assert tools()["demands.update"].risk == "high" and tools()["demands.update"].scope.version is not None
    assert all(key in tools() for key in spec.tools)


async def test_agent_drafts_are_hidden_from_the_client_portal(dorg: DOrg) -> None:
    from app.models.demands import Demand
    from app.services.demands import portal

    d = await _demand(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(Demand, uuid.UUID(d["id"]))
        requested = await portal._requested_by(db, [uuid.UUID(d["id"])])
    assert row is not None and row.status in portal.HIDDEN_UNLESS_REQUESTED
    assert requested.get(uuid.UUID(d["id"])) is None  # not raised by the client -> invisible in the portal


async def test_p1_jd_generation_stays_on_v1(dorg: DOrg) -> None:
    d = await _demand(dorg)
    with fake_llm([FakeTurn("emit_result", result={"content": "## Data Engineer\n\nBuild pipelines."})]):
        resp = await dorg.admin.client.post(
            f"{DEMANDS}/{d['id']}/jd-versions/generate", json={"kind": "full"}, headers=h(dorg.admin)
        )
    assert resp.status_code in (200, 201), resp.text
    async with tenant_session(dorg.tenant_id) as db:
        action = (
            (await db.execute(select(AIAction).where(AIAction.feature == "jd_generation"))).scalars().first()
        )
    assert action is not None and action.prompt_version == "demands_jd_generate.v1"


async def test_connected_mailbox_poll(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import connections_service
    from app.services.connections import fake as fake_provider
    from app.services.connections.provider import TokenSet
    from app.services.demand_intake import mailbox
    from app.services.demand_intake.receive import build_eml

    await disable_triggers(dorg, "intake_received", "draft_created")
    provider = fake_provider.instance()
    provider.reset()
    scopes: list[tuple[str, ...]] = [("mail.read",)]

    async def active(db: Any, user_id: Any, provider_name: Any = None) -> Any:
        return connections_service.ActiveConnection(
            uuid.uuid4(), "fake", provider, TokenSet("t", None, None, scopes[0]), "ada@fake.test"
        )

    monkeypatch.setattr(connections_service, "get_active", active)
    async with tenant_session(dorg.tenant_id) as db:
        from app.services.demand_intake.settings import row

        s = await row(db)
        s.intake_mode, s.mailbox_user_id = "connected_mailbox", dorg.admin.user_id
    raw = build_eml(
        from_addr="priya@acme.example",
        to="ada@fake.test",
        subject="Need 2 testers",
        body="Need 2 testers in Pune",
    )
    provider.add_folder_message("Talent OS Intake", raw, received_at=datetime.now(UTC) - timedelta(minutes=5))
    provider.add_folder_message("Other folder", raw)
    async with tenant_session(dorg.tenant_id) as db:
        assert await mailbox.poll_connected_mailbox(dorg.ctx(), db) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await mailbox.poll_connected_mailbox(dorg.ctx(), db) == 0  # cursor + idempotent ingest
        msgs = list((await db.execute(select(DemandIntakeMessage))).scalars())
    assert len(msgs) == 1 and msgs[0].source == "connected_mailbox" and msgs[0].status == "matched"
    scopes[0] = ("calendar",)
    async with tenant_session(dorg.tenant_id) as db:
        assert await mailbox.poll_connected_mailbox(dorg.ctx(), db) == 0
    resp = await dorg.admin.client.get(f"{DEMANDS}/intake-settings")
    assert "mailbox_needs_reconnect" in resp.json()["warnings"]


async def test_purge_after_180_days(app: FastAPI, dorg: DOrg) -> None:
    from app.services.demand_intake.items import purge_expired

    await disable_triggers(dorg, "intake_received", "draft_created")
    resp = await post_inbound(app, eml(dorg, sender="priya@acme.example", body="Need a tester"), dorg.address)
    mid = uuid.UUID(resp.json()["message_id"])
    async with tenant_session(dorg.tenant_id) as db:
        assert await purge_expired(db, at=datetime.now(UTC) + timedelta(days=181)) == 1
    row = await message_row(dorg, mid)
    assert (
        row.purged_at is not None
        and row.body_preview is None
        and row.extraction is None
        and row.status == "matched"
    )
