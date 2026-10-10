# ruff: noqa: E501
"""Epic 77 turns, availability, drafts, privacy, retention and erasure (77.1-77.5), Fake LLM only."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update

from app.core.database import OrgScope, tenant_session
from app.models.agent_runtime import AgentProposal, AgentRun
from app.models.portal_copilot import PortalCopilotConversation, PortalCopilotMessage, PortalCopilotSettings
from app.services import ai, portal_copilot_service
from app.services.portal_copilot import ratelimit
from tests.portal_copilot.conftest import BASE, PortalOrg, answer, ask, detail, new_conversation


async def _run(org: PortalOrg, run_id: str) -> AgentRun:
    async with tenant_session(org.tenant_id) as db:
        run = await db.get(AgentRun, uuid.UUID(run_id))
        assert run is not None
        db.expunge(run)
        return run


# ================================================================ status / principals
async def test_status_reasons(po: PortalOrg) -> None:
    ok = (await po.cu_a.client.get(f"{BASE['client']}/status")).json()
    assert ok["available"] is True and ok["questions_per_hour"] == 30 and "acting for" in ok["disclosure"]
    nouse = (await po.cu_a_nouse.client.get(f"{BASE['client']}/status")).json()
    assert nouse["reason"] == "no_permission"
    resp = await po.cu_a_nouse.client.post(f"{BASE['client']}/conversations", headers=po.cu_a_nouse.csrf)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "portal_permission_required"
    cand = (await po.cand_1.client.get(f"{BASE['candidate']}/status")).json()
    assert cand["available"] is True and cand["questions_per_hour"] == 15
    async with tenant_session(po.tenant_id) as db:
        await db.execute(update(PortalCopilotSettings).values(vendor_enabled=False))
    assert (await po.vu_x.client.get(f"{BASE['vendor']}/status")).json()["reason"] == "not_enabled"
    async with tenant_session(po.tenant_id) as db:
        await ai.set_features(db, po.tenant_id, {"agentic": False})
    assert (await po.cu_a.client.get(f"{BASE['client']}/status")).json()["reason"] == "agentic_off"


async def test_candidate_consent_gates_the_first_question(po: PortalOrg) -> None:
    async with tenant_session(po.tenant_id) as db:
        await db.execute(text("UPDATE consents SET status = 'withdrawn' WHERE candidate_id = :c AND purpose = 'ai_agent_communication'"), {"c": po.cand_1.id})
    st = (await po.cand_1.client.get(f"{BASE['candidate']}/status")).json()
    assert st["reason"] == "consent_required" and st["consent_notice_id"]
    resp = await po.cand_1.client.post(f"{BASE['candidate']}/conversations", headers=po.cand_1.csrf)
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "consent_required"
    resp = await po.cand_1.client.post(f"{BASE['candidate']}/consent", json={"notice_id": st["consent_notice_id"]}, headers=po.cand_1.csrf)
    assert resp.status_code == 200, resp.text
    assert resp.json()["available"] is True


async def test_kill_switch_pauses_copilot_not_the_portal(po: PortalOrg) -> None:
    async with tenant_session(po.tenant_id) as db:
        from app.models.ai import AITenantSettings

        await db.execute(update(AITenantSettings).values(kill_switch_at=datetime.now(UTC)))
    assert (await po.cu_a.client.get(f"{BASE['client']}/status")).json()["reason"] == "kill_switch"
    assert (await po.cu_a.client.get("/api/v1/client-portal/demands")).status_code == 200


# ================================================================ turns
async def test_turn_runs_as_portal_owner_and_stores_sealed_answer(po: PortalOrg) -> None:
    conv = await new_conversation(po.cu_a)
    turn, fake = await ask(
        po.cu_a, conv["id"], "What's the status of my open requirements?",
        [{"kind": "tool_calls", "calls": [["portal_client.list_demands", {}]]}, {"kind": "text", "text": "You have 2 requirements: [Alpha Data Engineer](/client/demands/x)."}],
    )  # fmt: skip
    assert turn["stream_url"] == f"/api/v1/client-portal/copilot/turns/{turn['run_id']}/events"
    run = await _run(po, turn["run_id"])
    assert run.status == "completed", (run.status, run.reason_code)
    assert (run.origin, run.trigger_kind, run.owner_user_id) == ("portal", "portal", None)
    assert (run.owner_principal_type, run.owner_principal_id, run.owner_org_id) == ("client_user", po.cu_a.id, po.client_a)
    names = {t["name"] for t in fake.calls[0]["tools"]}
    assert "portal_client__list_demands" in names and not any(n.startswith(("candidates__", "messaging__")) for n in names)
    msgs = await detail(po.cu_a, conv["id"])
    assert [m["role"] for m in msgs["messages"]] == ["user", "assistant"]
    ans = answer(msgs)["content"]
    assert ans["text"].startswith("You have 2") and ans["links"] == ["/client/demands/x"]
    assert msgs["title"] == "What's the status of my open requirements?"
    async with tenant_session(po.tenant_id) as db:  # content sealed at rest
        raw = list((await db.execute(select(PortalCopilotMessage.content_sealed).where(PortalCopilotMessage.conversation_id == uuid.UUID(conv["id"])))).scalars())
    assert raw and all(b"open requirements" not in bytes(r) and b"You have 2" not in bytes(r) for r in raw)
    # another principal (same org, other user) and another org: 404
    for other in (po.cu_a_restricted, po.cu_b):
        assert (await other.client.get(f"{BASE['client']}/conversations/{conv['id']}")).status_code == 404
        assert (await other.client.get(f"{BASE['client']}/turns/{turn['run_id']}/events")).status_code == 404


async def test_org_rls_hides_other_organisations_rows(po: PortalOrg) -> None:
    conv = await new_conversation(po.cu_a)
    async with tenant_session(po.tenant_id, org=OrgScope.of("client", po.client_b)) as db:
        assert (await db.get(PortalCopilotConversation, uuid.UUID(conv["id"]))) is None
    async with tenant_session(po.tenant_id, org=OrgScope.of("client", po.client_a)) as db:
        assert (await db.get(PortalCopilotConversation, uuid.UUID(conv["id"]))) is not None


async def test_rate_limit_candidate_15_per_hour(po: PortalOrg) -> None:
    conv = await new_conversation(po.cand_1)
    for _ in range(15):
        await ratelimit.hit(po.tenant_id, "candidate", po.cand_1.id, 15)
    resp, _ = await ask(po.cand_1, conv["id"], "When is my next interview?", expect=429)
    assert resp["error"]["code"] == "copilot_rate_limited"
    await ratelimit.reset(po.tenant_id, "candidate", po.cand_1.id)


async def test_idempotent_post_and_metering(po: PortalOrg, monkeypatch: object) -> None:
    from app.services import metering_service

    units: list[str] = []

    async def record(ctx: object, unit: str, *a: object, **k: object) -> None:
        units.append(unit)

    monkeypatch.setattr(metering_service, "record", record)  # type: ignore[attr-defined]
    conv = await new_conversation(po.vu_x)
    key = {"Idempotency-Key": uuid.uuid4().hex}
    from app.services.ai.fake_llm import fake_llm
    from tests.portal_copilot.conftest import script

    with fake_llm(script("vendor", {"kind": "text", "text": "One release is open."})):
        r1 = await po.vu_x.client.post(f"{BASE['vendor']}/conversations/{conv['id']}/messages", json={"text": "Which releases are open for us?"}, headers={**po.vu_x.csrf, **key})
        r2 = await po.vu_x.client.post(f"{BASE['vendor']}/conversations/{conv['id']}/messages", json={"text": "Which releases are open for us?"}, headers={**po.vu_x.csrf, **key})
    assert r1.status_code == r2.status_code == 200 and r1.json()["run_id"] == r2.json()["run_id"]
    assert units == ["portal_copilot_question"]


# ================================================================ drafts
async def _draft(po: PortalOrg, who: object, kind: str, tool: str, args: dict) -> str:
    conv = await new_conversation(who)  # type: ignore[arg-type]
    turn, _ = await ask(who, conv["id"], "please draft", [{"kind": "tool_calls", "calls": [[tool, args]]}, {"kind": "text", "text": "I've prepared a draft for you to review."}])  # type: ignore[arg-type]
    async with tenant_session(po.tenant_id) as db:
        p = (await db.execute(select(AgentProposal).where(AgentProposal.run_id == uuid.UUID(turn["run_id"])))).scalar_one()
        assert p.origin == "portal" and p.status == "pending" and p.owner_user_id is None
        return str(p.id)


async def test_client_message_draft_confirms_as_client_user(po: PortalOrg) -> None:
    pid = await _draft(po, po.cu_a, "client", "portal_client.draft_message", {"text": "Please call me about Alpha"})
    # not in the internal proposal inbox; staff cannot confirm it
    inbox = await po.admin.client.get("/api/v1/proposals")
    assert pid not in inbox.text
    staff = await po.admin.client.post(f"/api/v1/proposals/{pid}/confirm", headers={"X-CSRF-Token": po.admin.client.cookies.get("tos_csrf") or ""})
    assert staff.status_code in (403, 404, 405, 422)
    async with tenant_session(po.tenant_id) as db:
        assert (await db.get(AgentProposal, uuid.UUID(pid))).status == "pending"  # type: ignore[union-attr]
    # another principal cannot confirm
    other = await po.cu_b.client.post(f"{BASE['client']}/drafts/{pid}/confirm", headers=po.cu_b.csrf)
    assert other.status_code == 404
    # edits outside the whitelist are refused
    bad = await po.cu_a.client.post(f"{BASE['client']}/drafts/{pid}/confirm", json={"edits": {"tenant": "x"}}, headers=po.cu_a.csrf)
    assert bad.status_code == 422
    resp = await po.cu_a.client.post(f"{BASE['client']}/drafts/{pid}/confirm", json={"edits": {"text": "Please call me today"}}, headers=po.cu_a.csrf)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "executed", body
    async with tenant_session(po.tenant_id) as db:
        p = await db.get(AgentProposal, uuid.UUID(pid))
        assert p is not None and p.decided_by is None and p.args["text"] == "Please call me today"
        notes = (await db.execute(text("SELECT notes, entity_type FROM tasks WHERE id = :i"), {"i": uuid.UUID(body["result"]["reference"])})).one()
    assert notes[0] == "Please call me today" and notes[1] == "client"


async def test_vendor_profile_draft_returns_submit_link_only(po: PortalOrg) -> None:
    pid = await _draft(po, po.vu_x, "vendor", "portal_vendor.draft_candidate_profile", {"release_id": str(po.release_x), "full_name": "Nia Nair", "skills": ["Python"]})
    resp = await po.vu_x.client.post(f"{BASE['vendor']}/drafts/{pid}/confirm", headers=po.vu_x.csrf)
    assert resp.status_code == 200, resp.text
    assert resp.json()["open_url"] == f"/vendor/releases/{po.release_x}/submit?draft={pid}"


async def test_foreign_release_draft_is_not_created(po: PortalOrg) -> None:
    conv = await new_conversation(po.vu_x)
    turn, _ = await ask(po.vu_x, conv["id"], "submit for Y", [{"kind": "tool_calls", "calls": [["portal_vendor.draft_candidate_profile", {"release_id": str(po.release_y), "full_name": "Zed"}]]}, {"kind": "text", "text": "I couldn't find that."}])
    async with tenant_session(po.tenant_id) as db:
        assert (await db.execute(select(AgentProposal.id).where(AgentProposal.run_id == uuid.UUID(turn["run_id"])))).first() is None


async def test_candidate_handoff_draft_creates_task_and_discard(po: PortalOrg) -> None:
    pid = await _draft(po, po.cand_1, "candidate", "portal_candidate.draft_handoff", {"question": "Can I negotiate the salary?", "application_id": str(po.app_1)})
    resp = await po.cand_1.client.post(f"{BASE['candidate']}/drafts/{pid}/confirm", headers=po.cand_1.csrf)
    assert resp.status_code == 200 and resp.json()["status"] == "executed", resp.text
    pid2 = await _draft(po, po.cand_1, "candidate", "portal_candidate.draft_message", {"question": "Thanks!"})
    resp = await po.cand_1.client.post(f"{BASE['candidate']}/drafts/{pid2}/discard", headers=po.cand_1.csrf)
    assert resp.status_code == 200 and resp.json()["status"] == "discarded"
    assert (await po.cand_2.client.post(f"{BASE['candidate']}/drafts/{pid2}/confirm", headers=po.cand_2.csrf)).status_code == 404


async def test_expired_draft_cannot_be_confirmed(po: PortalOrg) -> None:
    pid = await _draft(po, po.cu_a, "client", "portal_client.draft_message", {"text": "Later"})
    async with tenant_session(po.tenant_id) as db:
        await db.execute(update(AgentProposal).where(AgentProposal.id == uuid.UUID(pid)).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    resp = await po.cu_a.client.post(f"{BASE['client']}/drafts/{pid}/confirm", headers=po.cu_a.csrf)
    assert resp.status_code == 409


# ================================================================ privacy / admin / retention / erasure
async def test_admin_sees_metadata_only_and_payload_is_private(po: PortalOrg) -> None:
    conv = await new_conversation(po.cu_a)
    turn, _ = await ask(po.cu_a, conv["id"], "secret question text", [{"kind": "text", "text": "secret answer text"}])
    usage = await po.admin.client.get("/api/v1/copilot/portal-admin/usage")
    assert usage.status_code == 200, usage.text
    assert "secret" not in usage.text
    row = next(r for r in usage.json()["rows"] if r["portal"] == "client")
    assert row["questions"] >= 1
    payload = await po.admin.client.post(f"/api/v1/agent-runs/{turn['run_id']}/payload", json={"reason": "Investigating a complaint"}, headers={"X-CSRF-Token": po.admin.client.cookies.get("tos_csrf") or ""})
    assert payload.status_code in (403, 404)
    if payload.status_code == 403:
        assert payload.json()["error"]["code"] == "copilot_private"


async def test_admin_settings_require_agentic(po: PortalOrg) -> None:
    from tests.portal_copilot.conftest import h

    got = await po.admin.client.get("/api/v1/copilot/portal-admin/settings")
    assert got.status_code == 200 and got.json()["client_enabled"] is True
    async with tenant_session(po.tenant_id) as db:
        await ai.set_features(db, po.tenant_id, {"agentic": False})
    resp = await po.admin.client.put("/api/v1/copilot/portal-admin/settings", json={"vendor_enabled": True, "retention_days": 14}, headers=h(po.admin))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "agentic_disabled"
    async with tenant_session(po.tenant_id) as db:
        await ai.set_features(db, po.tenant_id, {"agentic": True})
    resp = await po.admin.client.put("/api/v1/copilot/portal-admin/settings", json={"vendor_enabled": False, "retention_days": 14}, headers=h(po.admin))
    assert resp.status_code == 200 and resp.json()["retention_days"] == 14 and resp.json()["vendor_enabled"] is False
    async with tenant_session(po.tenant_id) as db:
        assert (await ai.features_map(db))["agent_copilot_vendor"] is False


async def test_purge_and_candidate_erasure(po: PortalOrg) -> None:
    conv = await new_conversation(po.cand_1)
    await ask(po.cand_1, conv["id"], "When is my next interview?")
    old = await new_conversation(po.cu_a)
    async with tenant_session(po.tenant_id) as db:
        await db.execute(update(PortalCopilotConversation).where(PortalCopilotConversation.id == uuid.UUID(old["id"])).values(expires_at=datetime.now(UTC) - timedelta(days=1)))
    assert await portal_copilot_service.purge_expired(po.tenant_id) >= 1
    from app.services.portal_copilot_erasure import PortalCopilotHandler
    from app.services.privacy.registry import ErasureContext

    handler = PortalCopilotHandler()
    ectx = ErasureContext(tenant_id=po.tenant_id, candidate_id=po.cand_1.id, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(po.tenant_id) as db:
        assert await handler.verify(db, ectx)
        counts = await handler.erase(db, ectx)
        assert counts["portal_copilot_conversations"] >= 1 and counts["portal_copilot_messages"] >= 2
        assert await handler.verify(db, ectx) == []
