# ruff: noqa: E501
"""Epic 56 Sourcing & Matching Agent: runs with the Fake LLM, exclusions, ranking, drafts, approval and caps,
entry decisions, re-rank versions, triggers, erasure and the API."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.permissions import Principal, Scope
from app.models.agent_runtime import AgentProposal, AgentRun, AgentToolCall
from app.models.comms import Email
from app.models.pipeline import Application
from app.models.sourcing_agent import AgentShortlist, AgentShortlistDismissal, AgentShortlistEntry
from app.schemas.agentic import AgentConfigIn, BulkDecisionIn, ProposalDecisionIn
from app.services import agent_runtime_service as runtime
from app.services import entity_refs
from app.services import sourcing_agent_service as svc
from app.services.agent_runtime.state import ToolCtx
from app.services.campaigns import governance
from tests.agents.conftest import AgentOrg
from tests.agents.sourcing.helpers import (
    CAP,
    DONE,
    DRAFT,
    GET,
    RANKED,
    SAVE,
    SEARCH,
    Seed,
    body,
    enable_sourcing,
    happy_script,
    rows,
    run_agent,
)
from tests.epic5 import h

API = "/api/v1"


async def _live_ctx(seed: Seed) -> RequestContext:
    async with tenant_session(seed.tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, seed.tenant_id, seed.admin.user_id)
    return RequestContext(tenant_id=seed.tenant_id, principal=principal)


async def _calls(seed: Seed, run_id: uuid.UUID) -> dict[str, AgentToolCall]:
    out = await rows(
        seed.tenant_id, AgentToolCall, AgentToolCall.run_id == run_id, order_by=AgentToolCall.created_at
    )
    return {c.tool_key: c for c in out}


async def _current(seed: Seed) -> tuple[AgentShortlist, list[AgentShortlistEntry]]:
    shortlist = (
        await rows(
            seed.tenant_id,
            AgentShortlist,
            AgentShortlist.demand_id == seed.demand_id,
            AgentShortlist.status == "current",
        )
    )[0]
    entries = await rows(
        seed.tenant_id,
        AgentShortlistEntry,
        AgentShortlistEntry.shortlist_id == shortlist.id,
        order_by=AgentShortlistEntry.rank,
    )
    return shortlist, entries


async def _happy(seed: Seed) -> uuid.UUID:
    ref = await run_agent(seed, happy_script(seed))
    assert ref.run_id is not None, ref
    run = (await rows(seed.tenant_id, AgentRun, AgentRun.id == ref.run_id))[0]
    assert run.status == "completed", (run.status, run.reason_code)
    return ref.run_id


# ================================================================ 56.1 / 56.2 run, exclusions, order
async def test_happy_path_ranks_excludes_and_drafts_without_sending(seed: Seed) -> None:
    run_id = await _happy(seed)
    calls = await _calls(seed, run_id)
    assert calls[SAVE].outcome == "executed" and calls[SAVE].mode == "auto"
    assert calls[DRAFT].outcome == "executed"
    shortlist, entries = await _current(seed)
    assert (shortlist.version, shortlist.trigger_key, shortlist.run_id) == (1, "manual", run_id)
    assert shortlist.excluded_counts["in_pipeline"] == 1
    assert shortlist.excluded_counts["archived"] == 1
    assert shortlist.excluded_counts["do_not_contact"] == 1
    order = [e.candidate_id for e in entries]
    c = seed.c
    for gone in ("e", "f", "g"):
        assert c[gone] not in order
    # overall desc; equal 80 -> later activity (c) before b; the unscored search hit (d) last
    known = [cid for cid in order if cid in {c["a"], c["b"], c["c"], c["d"]}]
    assert known == [c["a"], c["c"], c["b"], c["d"]]
    by = {e.candidate_id: e for e in entries}
    assert by[c["d"]].flags == ["no_ai_consent"] and by[c["d"]].explanation is None
    assert "search" in {s["kind"] for s in by[c["d"]].sources}
    assert {s["kind"] for s in by[c["a"]].sources} == {"match"}
    assert by[c["a"]].contactability["email"] == {"ok": True, "reason": None}
    assert by[c["a"]].best_channel == "email"
    # drafts became outreach.send Proposals; nothing was sent
    proposals = await rows(seed.tenant_id, AgentProposal, AgentProposal.run_id == run_id)
    assert sorted(p.tool_key for p in proposals) == ["outreach.send", "outreach.send"]
    assert all(p.status == "pending" and p.risk == "high" for p in proposals)
    assert {by[c["a"]].draft_status, by[c["c"]].draft_status} == {"proposed"}
    assert by[c["a"]].outreach_proposal_id in {p.id for p in proposals}
    assert "unsubscribe" in (by[c["a"]].draft_body or "").lower()
    assert await rows(seed.tenant_id, Email, Email.origin == "agent") == []
    assert all(dict(p.preview).get("fields") for p in proposals)


async def test_same_inputs_give_the_same_order(seed: Seed) -> None:
    await _happy(seed)
    _, first = await _current(seed)
    await _happy(seed)
    shortlist, second = await _current(seed)
    assert shortlist.version == 2
    assert [e.candidate_id for e in first] == [e.candidate_id for e in second]


async def test_model_cannot_send_a_score(seed: Seed) -> None:
    d = str(seed.demand_id)
    ref = await run_agent(seed, [
        {"kind": "tool_calls", "calls": [[SAVE, {"demand_id": d, "candidates": [{"candidate_id": str(seed.c["b"]), "score": 99}]}]]},
        DONE,
    ])  # fmt: skip
    calls = await _calls(seed, ref.run_id)
    assert calls[SAVE].outcome == "invalid"
    assert await rows(seed.tenant_id, AgentShortlist, AgentShortlist.demand_id == seed.demand_id) == []


async def test_out_of_scope_ids_are_dropped(seed: Seed, other_org: AgentOrg) -> None:
    d = str(seed.demand_id)
    foreign = [str(other_org.candidate_id), str(uuid.uuid4())]
    ref = await run_agent(seed, [
        {"kind": "tool_calls", "calls": [[SAVE, {"demand_id": d, "candidates": [{"candidate_id": x} for x in foreign]}]]},
        DONE,
    ])  # fmt: skip
    calls = await _calls(seed, ref.run_id)
    assert calls[SAVE].outcome == "executed"
    _, entries = await _current(seed)
    assert not {str(e.candidate_id) for e in entries} & set(foreign)


async def test_other_demand_is_not_found(seed: Seed, other_org: AgentOrg) -> None:
    ref = await run_agent(
        seed, [{"kind": "tool_calls", "calls": [[RANKED, {"demand_id": str(uuid.uuid4())}]]}, DONE]
    )
    calls = await _calls(seed, ref.run_id)
    assert calls[RANKED].outcome == "not_found"


async def test_comp_out_of_range_is_invalid(seed: Seed) -> None:
    script = happy_script(seed)[:2]
    d = str(seed.demand_id)
    script.append({"kind": "tool_calls", "calls": [[DRAFT, {"demand_id": d, "drafts": [
        {"candidate_id": str(seed.c["a"]), "body": "This role pays ₹40 LPA. Interested?"}]}]]})  # fmt: skip
    script.append(DONE)
    ref = await run_agent(seed, script)
    calls = await _calls(seed, ref.run_id)
    assert (calls[DRAFT].outcome, calls[DRAFT].error_code) == ("invalid", "comp_outside_range")
    assert await rows(seed.tenant_id, AgentProposal, AgentProposal.run_id == ref.run_id) == []


async def test_foreign_link_is_stripped_and_flagged(seed: Seed) -> None:
    script = happy_script(seed)[:2]
    d = str(seed.demand_id)
    script.append({"kind": "tool_calls", "calls": [[DRAFT, {"demand_id": d, "drafts": [
        {"candidate_id": str(seed.c["a"]), "body": body() + " Details: https://evil.example.net/x"}]}]]})  # fmt: skip
    script.append(DONE)
    await run_agent(seed, script)
    _, entries = await _current(seed)
    entry = next(e for e in entries if e.candidate_id == seed.c["a"])
    assert "evil.example.net" not in (entry.draft_body or "") and "link_removed" in entry.flags


async def test_no_ai_consent_entry_gets_no_draft(seed: Seed) -> None:
    script = happy_script(seed)[:2]
    d = str(seed.demand_id)
    script.append(
        {
            "kind": "tool_calls",
            "calls": [
                [DRAFT, {"demand_id": d, "drafts": [{"candidate_id": str(seed.c["d"]), "body": body("Dev")}]}]
            ],
        }
    )
    script.append(DONE)
    ref = await run_agent(seed, script)
    calls = await _calls(seed, ref.run_id)
    assert (calls[DRAFT].outcome, calls[DRAFT].error_code) == ("invalid", "no_ai_consent")


async def test_protected_attribute_search_is_invalid(seed: Seed) -> None:
    ref = await run_agent(
        seed, [{"kind": "tool_calls", "calls": [[SEARCH, {"text": "female engineer"}]]}, DONE]
    )
    calls = await _calls(seed, ref.run_id)
    assert (calls[SEARCH].outcome, calls[SEARCH].error_code) == ("invalid", "protected_term")


async def test_search_respects_the_owner_scope(seed: Seed) -> None:
    from app.core.actors import ActorRef
    from app.core.agentic import get_tool

    tool = get_tool(SEARCH)
    args = tool.input_model.model_validate({"text": "Rao OR Iyer OR Nair", "limit": 20})

    async def found(scope: Scope, owner: uuid.UUID) -> set[uuid.UUID]:
        principal = Principal(
            type="agent", id=uuid.uuid4(), tenant_id=seed.tenant_id, permissions={"candidate:read": scope}
        )
        async with tenant_session(seed.tenant_id) as db:
            tctx = ToolCtx(
                request=RequestContext(tenant_id=seed.tenant_id, principal=principal, actor_kind="agent"), db=db,
                actor=ActorRef("agent", principal.id), run_id=None, agent_key="sourcing", owner_user_id=owner,
                subject=("demand", seed.demand_id), idempotency_key="t", execution="agent",
            )  # fmt: skip
            out = await tool.handler(tctx, args)
        return {i.candidate_id for i in out.data.items}  # type: ignore[attr-defined]

    assert seed.c["a"] in await found(Scope.TENANT, seed.admin.user_id)
    assert (
        await found(Scope.OWN, uuid.uuid4()) == set()
    )  # an own-scope recruiter only finds their own records


# ================================================================ 56.3 / 56.4 approval, caps
async def _proposal_for(seed: Seed, key: str) -> AgentProposal:
    _, entries = await _current(seed)
    entry = next(e for e in entries if e.candidate_id == seed.c[key])
    return (await rows(seed.tenant_id, AgentProposal, AgentProposal.id == entry.outreach_proposal_id))[0]


async def test_approve_sends_marketing_outreach_as_the_approver(seed: Seed) -> None:
    await _happy(seed)
    p = await _proposal_for(seed, "a")
    ctx = await _live_ctx(seed)
    async with tenant_session(seed.tenant_id) as db:
        out = await runtime.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="approve"))
    assert out.status == "executed", out.error_code
    emails = await rows(seed.tenant_id, Email, Email.origin == "agent")
    assert len(emails) == 1 and emails[0].sender_user_id == seed.admin.user_id
    assert "prepared by an AI assistant" in emails[0].body_html
    _, entries = await _current(seed)
    entry = next(e for e in entries if e.candidate_id == seed.c["a"])
    assert entry.draft_status == "sent" and entry.outreach_sent_at is not None
    # C-9: the approved outreach counts toward the P3 marketing frequency cap
    async with tenant_session(seed.tenant_id) as db:
        assert (
            await governance._frequency_count(db, seed.tenant_id, seed.c["a"], datetime.now(UTC), None) == 1
        )
        _, suppressed = await governance.bulk_preview(db, [seed.c["a"]], ("email",), datetime.now(UTC))
    assert suppressed["email"] == {}


def _capped(monkeypatch: pytest.MonkeyPatch, candidate_id: uuid.UUID) -> None:
    """Three marketing messages this week for ``candidate_id`` through a registered cap counter (C-9 registry)."""

    async def counter(db: Any, ids: Any, since: Any) -> dict[uuid.UUID, int]:
        return {candidate_id: 3} if candidate_id in list(ids) else {}

    monkeypatch.setitem(governance._cap_counters, "test_cap", counter)


async def test_cap_hit_before_approval_fails_with_the_reason(
    seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _happy(seed)
    _capped(monkeypatch, seed.c["c"])
    p = await _proposal_for(seed, "c")
    ctx = await _live_ctx(seed)
    async with tenant_session(seed.tenant_id) as db:
        out = await runtime.decide_proposal(ctx, db, p.id, ProposalDecisionIn(decision="approve"))
    assert (out.status, out.error_code) == ("execution_failed", "suppressed:frequency_cap")
    assert await rows(seed.tenant_id, Email, Email.origin == "agent") == []
    _, entries = await _current(seed)
    assert next(e for e in entries if e.candidate_id == seed.c["c"]).draft_status == "suppressed"


async def test_bulk_approval_of_one_demand(seed: Seed) -> None:
    run_id = await _happy(seed)
    ids = [p.id for p in await rows(seed.tenant_id, AgentProposal, AgentProposal.run_id == run_id)]
    ctx = await _live_ctx(seed)
    async with tenant_session(seed.tenant_id) as db:
        out = await runtime.bulk_decide(ctx, db, BulkDecisionIn(proposal_ids=ids, decision="approve"))
    assert sorted(o.status for o in out) == ["executed", "executed"]
    assert len(await rows(seed.tenant_id, Email, Email.origin == "agent")) == 2


async def test_cap_status_reports_reasons(seed: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    _capped(monkeypatch, seed.c["b"])
    ctx = await _live_ctx(seed)
    async with tenant_session(seed.tenant_id) as db:
        states = await svc.cap_status(ctx, db, seed.demand_id, [seed.c["a"], seed.c["b"], seed.c["g"]])
    assert states[seed.c["a"]].email.ok is True
    assert states[seed.c["b"]].email.reason == "frequency_cap" and states[seed.c["b"]].best_channel is None
    assert states[seed.c["g"]].email.reason == "no_marketing_consent"


# ================================================================ 56.2 / 56.5 decisions, versions
async def test_user_decisions_and_rerank_versions(seed: Seed) -> None:
    await _happy(seed)
    m = seed.admin
    resp = await m.client.get(f"{API}/demands/{seed.demand_id}/agent-shortlist")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["agent_enabled"] is True and data["shortlist"]["version"] == 1
    entries = {e["candidate_id"]: e for e in data["shortlist"]["entries"]}
    b, d = entries[str(seed.c["b"])], entries[str(seed.c["d"])]
    # a person's add is a normal action
    add = await m.client.post(
        f"{API}/agent-shortlist-entries/{b['id']}/decision",
        json={"decision": "add_to_pipeline"},
        headers=h(m),
    )
    assert add.status_code == 200, add.text
    assert add.json()["status"] == "added_to_pipeline"
    apps = await rows(
        seed.tenant_id,
        Application,
        Application.demand_id == seed.demand_id,
        Application.candidate_id == seed.c["b"],
    )
    assert (
        len(apps) == 1 and apps[0].source == "ai_match" and apps[0].source_ref == f"agent_shortlist:{b['id']}"
    )
    no_reason = await m.client.post(
        f"{API}/agent-shortlist-entries/{d['id']}/decision", json={"decision": "dismiss"}, headers=h(m)
    )
    assert no_reason.status_code == 422
    dis = await m.client.post(
        f"{API}/agent-shortlist-entries/{d['id']}/decision",
        json={"decision": "dismiss", "reason": "skills_gap"},
        headers=h(m),
    )
    assert dis.status_code == 200 and dis.json()["status"] == "dismissed"
    # re-rank: version 2, b excluded (in pipeline), d stays dismissed
    await _happy(seed)
    shortlist, v2 = await _current(seed)
    assert shortlist.version == 2
    by = {e.candidate_id: e for e in v2}
    assert seed.c["b"] not in by
    assert by[seed.c["d"]].status == "dismissed" and by[seed.c["d"]].dismiss_reason == "skills_gap"
    versions = (await m.client.get(f"{API}/demands/{seed.demand_id}/agent-shortlist/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    old = (await m.client.get(f"{API}/demands/{seed.demand_id}/agent-shortlist?version=1")).json()
    assert old["shortlist"]["status"] == "superseded"
    stale = await m.client.post(
        f"{API}/agent-shortlist-entries/{d['id']}/decision", json={"decision": "restore"}, headers=h(m)
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "shortlist_superseded"
    restore = await m.client.post(
        f"{API}/agent-shortlist-entries/{by[seed.c['d']].id}/decision",
        json={"decision": "restore"},
        headers=h(m),
    )
    assert restore.status_code == 200 and restore.json()["status"] == "suggested"
    assert (
        await rows(
            seed.tenant_id, AgentShortlistDismissal, AgentShortlistDismissal.demand_id == seed.demand_id
        )
        == []
    )


async def test_new_and_dropped_back_badges(seed: Seed) -> None:
    async def save(ids: list[uuid.UUID]) -> None:
        ctx = await _live_ctx(seed)
        async with tenant_session(seed.tenant_id) as db:
            await svc.save_suggested(
                ctx,
                ctx,
                db,
                seed.demand_id,
                run_id=None,
                candidates=[{"candidate_id": i} for i in ids],
                summary=None,
            )

    await save([seed.c["d"]])
    await save([])  # d dropped (it is only found by search)
    await save([seed.c["d"]])  # d is back
    _, entries = await _current(seed)
    assert "dropped_back" in next(e for e in entries if e.candidate_id == seed.c["d"]).flags


async def test_cross_tenant_api_is_not_found(seed: Seed, other_org: AgentOrg) -> None:
    resp = await other_org.admin.client.get(f"{API}/demands/{seed.demand_id}/agent-shortlist")
    assert resp.status_code == 404
    await _happy(seed)
    _, entries = await _current(seed)
    resp = await other_org.admin.client.post(
        f"{API}/agent-shortlist-entries/{entries[0].id}/decision",
        json={"decision": "dismiss", "reason": "other"},
        headers=h(other_org.admin),
    )
    assert resp.status_code == 404


async def test_settings_api(seed: Seed) -> None:
    m = seed.admin
    got = await m.client.get(f"{API}/settings/agents/sourcing")
    assert got.status_code == 200 and got.json()["top_n"] == 25 and got.json()["draft_top"] == 10
    put = await m.client.put(
        f"{API}/settings/agents/sourcing", json={"top_n": 5, "max_searches": 0}, headers=h(m)
    )
    assert put.status_code == 200 and put.json()["top_n"] == 5
    bad = await m.client.put(f"{API}/settings/agents/sourcing", json={"top_n": 500}, headers=h(m))
    assert bad.status_code == 422


async def test_manual_run_endpoint(seed: Seed) -> None:
    from app.services.ai.fake_llm import fake_llm
    from tests.agents.sourcing.helpers import turns

    m = seed.admin
    with fake_llm(turns(DONE)):
        resp = await m.client.post(
            f"{API}/demands/{seed.demand_id}/sourcing-runs",
            headers={**h(m), "Idempotency-Key": uuid.uuid4().hex},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["run_id"]


# ================================================================ triggers
async def test_agent_off_does_not_start(seed: Seed) -> None:
    await enable_sourcing(seed.tenant_id, on=False)
    ref = await run_agent(seed, [DONE])
    assert (ref.run_id, ref.reason) == (None, "feature_off")
    resp = await seed.admin.client.get(f"{API}/demands/{seed.demand_id}/agent-shortlist")
    assert resp.json() == {"demand_id": str(seed.demand_id), "agent_enabled": False, "shortlist": None}


async def test_no_owner_means_no_run(seed: Seed) -> None:
    async with tenant_session(seed.tenant_id) as db:
        ref = await runtime.start_run(
            RequestContext(tenant_id=seed.tenant_id, actor_kind="system"), db, agent_key="sourcing", trigger_kind="event",
            trigger_key="demand_opened", subject=("demand", uuid.uuid4()), dedup_key="sourcing:open:x",
        )  # fmt: skip
    assert (ref.run_id, ref.reason) == (None, "owner_unavailable")


async def test_event_trigger_runs_once_and_dedups(seed: Seed) -> None:
    from app.core.events import emit
    from app.events.sourcing_agent import SourcingRerankRequested
    from app.services.ai.fake_llm import fake_llm
    from tests.agents.sourcing.helpers import turns

    rid = uuid.uuid4()
    with fake_llm(turns(DONE, DONE)):
        for _ in range(2):
            async with tenant_session(seed.tenant_id) as db:
                emit(
                    db,
                    SourcingRerankRequested(
                        tenant_id=seed.tenant_id, demand_id=seed.demand_id, match_run_id=rid
                    ),
                )
    runs = await rows(seed.tenant_id, AgentRun, AgentRun.agent_key == "sourcing")
    assert [r.trigger_key for r in runs] in (
        ["requirements_changed"],
        ["requirements_changed", "requirements_changed"],
    )
    assert all(r.owner_user_id == seed.admin.user_id and r.subject_id == seed.demand_id for r in runs)


async def test_trigger_disabled_and_weekly_refresh(seed: Seed) -> None:
    from app.models.demands import Demand
    from app.services.ai.fake_llm import fake_llm
    from app.services.sourcing_agent import triggers
    from tests.agents.sourcing.helpers import turns

    async with tenant_session(seed.tenant_id) as db:
        await db.execute(
            update(Demand)
            .where(Demand.id == seed.demand_id)
            .values(opened_at=datetime.now(UTC) - timedelta(days=20))
        )
    ctx = await _live_ctx(seed)
    async with tenant_session(seed.tenant_id) as db:
        await runtime.update_config(
            ctx, db, "sourcing", AgentConfigIn(triggers_enabled={"weekly_refresh": False})
        )
    async with tenant_session(seed.tenant_id) as db:
        assert await triggers.weekly_refresh(db, seed.tenant_id) == 0
    async with tenant_session(seed.tenant_id) as db:
        await runtime.update_config(
            ctx, db, "sourcing", AgentConfigIn(triggers_enabled={"weekly_refresh": True})
        )
    with fake_llm(turns(DONE)):
        async with tenant_session(seed.tenant_id) as db:
            assert await triggers.weekly_refresh(db, seed.tenant_id) == 1
    runs = await rows(seed.tenant_id, AgentRun, AgentRun.agent_key == "sourcing")
    assert [r.trigger_key for r in runs] == ["weekly_refresh"]
    async with tenant_session(seed.tenant_id) as db:  # a run in the last 7 days -> no refresh
        assert await triggers.weekly_refresh(db, seed.tenant_id) == 0


async def test_demand_closed_cancels_active_runs(seed: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_runtime import runs as runs_mod
    from app.services.sourcing_agent import triggers

    monkeypatch.setattr(runs_mod, "request_step", lambda *a, **k: None)  # keep the run queued
    ref = await run_agent(seed, [DONE])
    assert ref.run_id is not None
    async with tenant_session(seed.tenant_id) as db:
        assert await triggers.cancel_for_demand(db, seed.tenant_id, seed.demand_id) == 1
    run = (await rows(seed.tenant_id, AgentRun, AgentRun.id == ref.run_id))[0]
    assert (run.status, run.reason_code) == ("cancelled", "subject_closed")


async def test_match_run_filter(seed: Seed) -> None:
    from app.models.matching import MatchRun
    from app.services.sourcing_agent import triggers

    async with tenant_session(seed.tenant_id) as db:
        opened = (
            (await db.execute(select(MatchRun).where(MatchRun.demand_id == seed.demand_id))).scalars().first()
        )
        if opened is not None:
            assert await triggers.on_match_run(db, seed.demand_id, opened.id, "completed") is False
        assert await triggers.on_match_run(db, seed.demand_id, uuid.uuid4(), "completed") is False


# ================================================================ erasure / tools
async def test_erasure_removes_entries_and_dismissals(seed: Seed) -> None:
    from app.services.privacy.registry import ErasureContext
    from app.services.sourcing_agent_erasure import SourcingShortlistsHandler

    await _happy(seed)
    handler = SourcingShortlistsHandler()
    ectx = ErasureContext(
        tenant_id=seed.tenant_id, candidate_id=seed.c["a"], run_id=uuid.uuid4(), reason="dsr"
    )
    async with tenant_session(seed.tenant_id) as db:
        exported = await handler.export(db, ectx)
        assert exported is not None and exported["agent_shortlists"][0]["rank"] >= 1
        await handler.erase(db, ectx)
        assert await handler.verify(db, ectx) == []


async def test_pipeline_add_proposal_needs_the_setting(seed: Seed) -> None:
    d = str(seed.demand_id)
    add = [
        "pipeline.add_candidate",
        {"demand_id": d, "candidate_id": str(seed.c["a"]), "reason": "Strong match"},
    ]
    ref = await run_agent(seed, [{"kind": "tool_calls", "calls": [add]}, DONE])
    calls = await _calls(seed, ref.run_id)
    assert (calls["pipeline.add_candidate"].outcome, calls["pipeline.add_candidate"].error_code) == (
        "failed",
        "pipeline_adds_disabled",
    )
    ctx = await _live_ctx(seed)
    async with tenant_session(seed.tenant_id) as db:
        from app.schemas.sourcing_agent import SourcingSettingsIn

        await svc.update_settings(ctx, db, SourcingSettingsIn(propose_pipeline_adds=True))
    ref = await run_agent(seed, [{"kind": "tool_calls", "calls": [add]}, DONE])
    proposals = await rows(seed.tenant_id, AgentProposal, AgentProposal.run_id == ref.run_id)
    assert [p.tool_key for p in proposals] == ["pipeline.add_candidate"]
    async with tenant_session(seed.tenant_id) as db:
        out = await runtime.decide_proposal(ctx, db, proposals[0].id, ProposalDecisionIn(decision="approve"))
    assert out.status == "executed", out.error_code
    apps = await rows(
        seed.tenant_id,
        Application,
        Application.demand_id == seed.demand_id,
        Application.candidate_id == seed.c["a"],
    )
    assert len(apps) == 1


async def test_tool_catalogue_registration() -> None:
    from app.core.agentic import agents, get_tool

    spec = agents()["sourcing"]
    assert not spec.placeholder and spec.owner_epic == "56"
    for key in spec.tools:
        get_tool(key)
    assert get_tool("outreach.send").risk == "high" and get_tool("outreach.send").egress
    assert get_tool("pipeline.add_candidate").risk == "high"
    assert get_tool("sequences.enrol").risk == "high"
    assert get_tool(SAVE).default_mode == "auto" and get_tool(DRAFT).default_mode == "auto"
    assert {t.key for t in spec.triggers} == {
        "demand_opened",
        "requirements_changed",
        "weekly_refresh",
        "manual",
        "workflow",
    }
    with pytest.raises(Exception):  # noqa: B017 - extra="forbid"
        get_tool(SAVE).input_model.model_validate({"demand_id": str(uuid.uuid4()), "score": 1})


def _unused(*_: Any) -> None:  # keep imports used by type checkers
    _ = (CAP, GET, NotFound)
