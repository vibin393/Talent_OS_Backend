# ruff: noqa: E501, ANN001, RUF015
"""Epic 59 stories 59.1-59.4 end to end with the Fake LLM: propose, offer, pick, book through P1, duplicate picks,
conflicts and re-offers, counter-proposals, max rounds, partial results, reschedules, policy (Proposals), stage exit."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.models.agent_runtime import AgentProposal
from app.models.comms import Email
from app.models.interviews import Interview
from app.models.scheduling_agent import SchedulingRequestEvent
from app.models.tasks import Task
from app.schemas.scheduling_agent import RequestStartIn, SlotProposalIn
from app.schemas.scheduling_public import CounterWindowIn
from app.services import scheduling_agent_service as svc
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.connections.provider import BusyBlock
from tests.agents.scheduling.conftest import (
    API,
    KOrg,
    calls,
    done,
    offer_script,
    offers_of,
    request_of,
    runs_of,
    tool_calls,
    wait,
)
from tests.modules.identity.conftest import csrf, join

LINK_RE = re.compile(r"/l/([A-Za-z0-9_\-]{20,})")
BENIGN = FakeTurn("emit_result", result={"verdict": "benign", "reasons": []})


async def _start(
    k: KOrg,
    fake,
    offer_id: uuid.UUID,
    *,
    participants: list[uuid.UUID] | None = None,
    extra=None,
    channel: str = "email",
) -> uuid.UUID:
    async with tenant_session(k.tenant_id) as db:
        req = await svc.requests.start(
            k.ctx(),
            db,
            k.application_id,
            RequestStartIn(stage_id=k.stage_id, participant_user_ids=participants or [k.admin.user_id]),
        )
        rid = req.id
        fake._by_prompt["agent_scheduling"].extend(offer_script(k, rid, offer_id, channel))
        if extra is not None:
            fake._by_prompt["agent_scheduling"].extend(extra(rid))
    return rid


async def _token(k: KOrg) -> str:
    async with tenant_session(k.tenant_id) as db:
        mails = (await db.execute(select(Email).order_by(Email.created_at.desc()))).scalars().all()
        for m in mails:
            found = LINK_RE.search(m.body_html or "")
            if found:
                return found.group(1)
    raise AssertionError("no self-scheduling link was emailed")


async def _history(k: KOrg, rid: uuid.UUID) -> list[str]:
    async with tenant_session(k.tenant_id) as db:
        return list(
            (
                await db.execute(
                    select(SchedulingRequestEvent.kind)
                    .where(SchedulingRequestEvent.request_id == rid)
                    .order_by(SchedulingRequestEvent.at, SchedulingRequestEvent.created_at)
                )
            ).scalars()
        )


def _script() -> dict:
    return {"agent_scheduling": [], None: [BENIGN] * 10}


# ================================================================ happy path, duplicate pick
async def test_offer_pick_books_through_p1_and_duplicate_pick_dedups(
    korg: KOrg, offer_ids: list[uuid.UUID]
) -> None:
    k = korg
    with fake_llm(_script()) as fake:
        rid = await _start(
            k,
            fake,
            offer_ids[0],
            extra=lambda rid: [
                calls(("interviews.book", {"request_id": str(rid), "offer_id": str(offer_ids[0])})),
                done("booked"),
            ],
        )
        req = await request_of(k, rid)
        assert req.status == "slots_offered" and req.round == 1 and req.human_id.startswith("SCH-")
        offers = await offers_of(k, rid)
        assert (
            len(offers) == 1
            and offers[0].id == offer_ids[0]
            and offers[0].status == "open"
            and 1 <= len(offers[0].slots) <= 5
        )
        token = await _token(k)
        # the public page shows the slots in the candidate's zone
        view = await k.admin.client.get(f"{API}/public/slot-pick/{token}")
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["status"] == "open" and body["candidate_timezone"] == "Asia/Kolkata" and body["slots"]
        assert "participants" not in body and body["stage_name"]
        pick_id = str(uuid.uuid4())
        resp = await k.admin.client.post(
            f"{API}/public/slot-pick/{token}/pick", json={"index": 0, "pick_id": pick_id}
        )
        assert resp.status_code == 200, resp.text
    req = await request_of(k, rid)
    assert req.status == "booked" and req.booked_interview_id is not None
    async with tenant_session(k.tenant_id) as db:
        interview = await db.get(Interview, req.booked_interview_id)
        assert (
            interview is not None
            and interview.status == "scheduled"
            and interview.organizer_user_id == k.admin.user_id
        )
        assert interview.starts_at == datetime.fromisoformat(offers[0].slots[0]["start"])
        count = len(
            (await db.execute(select(Interview).where(Interview.application_id == k.application_id)))
            .scalars()
            .all()
        )
    assert count == 1
    # a duplicate pick (same pick_id, or the same slot again) returns the same booking
    again = await k.admin.client.post(
        f"{API}/public/slot-pick/{token}/pick", json={"index": 0, "pick_id": pick_id}
    )
    assert (
        again.status_code == 200
        and again.json()["deduplicated"] is True
        and again.json()["status"] == "booked"
    )
    async with tenant_session(k.tenant_id) as db:
        assert (
            len(
                (await db.execute(select(Interview).where(Interview.application_id == k.application_id)))
                .scalars()
                .all()
            )
            == 1
        )
    assert {"requested", "slots_computed", "offered", "picked", "booked"} <= set(await _history(k, rid))
    runs = await runs_of(k)
    calls_ = await tool_calls(k, runs[0].id)
    by_key = {c.tool_key: c for c in calls_}
    assert (
        by_key["scheduling.offer_slots"].outcome == "executed"
        and by_key["scheduling.offer_slots"].mode == "auto"
    )
    assert by_key["interviews.book"].outcome == "executed" and by_key["interviews.book"].mode == "auto"


async def test_propose_contract(korg: KOrg) -> None:
    k = korg
    async with tenant_session(k.tenant_id) as db:
        out = await svc.propose(
            k.ctx(), db,
            SlotProposalIn(application_id=k.application_id, interview_stage_id=k.stage_id, duration_minutes=60, participant_user_ids=(k.admin.user_id,), candidate_timezone="Asia/Kolkata", max_slots=3),
        )  # fmt: skip
        assert 1 <= len(out.slots) <= 3 and not out.partial and out.unknown_participants == ()
        assert all(s.all_known_free and s.end - s.start == timedelta(minutes=60) for s in out.slots)
        from app.models.scheduling_agent import SchedulingSlotOffer

        offer = await db.get(SchedulingSlotOffer, out.proposal_ref)
        assert offer is not None and offer.status == "draft"


# ================================================================ unknown calendar, partial
async def test_unknown_calendar_interviewer_gets_availability_request(
    app, korg: KOrg, offer_ids: list[uuid.UUID]
) -> None:
    k = korg
    other = await join(app, k.porg.tenant, name="Una Unconnected")
    try:
        with fake_llm(_script()) as fake:

            def extra(rid: uuid.UUID) -> list:
                return [
                    calls(
                        (
                            "notify.user",
                            {
                                "user_id": str(other.user_id),
                                "entity_type": "application",
                                "entity_id": str(k.application_id),
                                "message": "Please connect your calendar or confirm availability.",
                            },
                        )
                    ),
                    done("waiting"),
                ]

            async with tenant_session(k.tenant_id) as db:
                req = await svc.requests.start(
                    k.ctx(),
                    db,
                    k.application_id,
                    RequestStartIn(
                        stage_id=k.stage_id, participant_user_ids=[k.admin.user_id, other.user_id]
                    ),
                )
                rid = req.id
                fake._by_prompt["agent_scheduling"].extend([
                    calls(("interviews.participants", {"request_id": str(rid)})),
                    calls(("scheduling.propose", {"request_id": str(rid)})),
                    calls(("scheduling.offer_slots", {"offer_id": str(offer_ids[0]), "candidate_id": str(k.candidate_id), "channel": "email"})),
                    *extra(rid),
                ])  # fmt: skip
        offer = (await offers_of(k, rid))[0]
        assert (
            other.user_id in offer.unknown_participants
            and offer.slots
            and offer.slots[0]["unknown_participants"] == [str(other.user_id)]
        )
        runs = await runs_of(k)
        outcomes = {c.tool_key: c.outcome for c in await tool_calls(k, runs[0].id)}
        assert outcomes["notify.user"] == "executed" and outcomes["scheduling.offer_slots"] == "executed"
        detail = await k.admin.client.get(f"{API}/scheduling-requests/{rid}")
        assert detail.status_code == 200
        parts = {p["user_id"]: p for p in detail.json()["participants"]}
        assert (
            parts[str(other.user_id)]["calendar_connected"] is False
            and parts[str(k.admin.user_id)]["calendar_connected"] is True
        )
    finally:
        await other.client.aclose()


async def test_no_common_slot_is_a_partial_proposal(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    now = datetime.now(UTC)
    k.fake.busy[k.admin.email.lower()] = [BusyBlock(now - timedelta(days=1), now + timedelta(days=40))]
    with fake_llm(_script()) as fake:
        async with tenant_session(k.tenant_id) as db:
            req = await svc.requests.start(
                k.ctx(),
                db,
                k.application_id,
                RequestStartIn(stage_id=k.stage_id, participant_user_ids=[k.admin.user_id]),
            )
            rid = req.id
            fake._by_prompt["agent_scheduling"].extend([
                calls(("scheduling.propose", {"request_id": str(rid)})),
                calls(("scheduling.offer_slots", {"offer_id": str(offer_ids[0]), "candidate_id": str(k.candidate_id), "channel": "email"})),
                calls(("scheduling.offer_partial", {"offer_id": str(offer_ids[0]), "candidate_id": str(k.candidate_id), "channel": "email", "note_to_recruiter": "Ada is busy all fortnight"})),
                done("needs_human"),
            ])  # fmt: skip
    offer = (await offers_of(k, rid))[0]
    assert offer.partial and offer.status == "draft"
    runs = await runs_of(k)
    calls_ = {c.tool_key: c for c in await tool_calls(k, runs[0].id)}
    assert (
        calls_["scheduling.offer_slots"].outcome == "failed"
        and calls_["scheduling.offer_slots"].error_code == "use_offer_partial"
    )
    assert calls_["scheduling.offer_partial"].outcome == "proposed"
    async with tenant_session(k.tenant_id) as db:
        props = (
            (await db.execute(select(AgentProposal).where(AgentProposal.run_id == runs[0].id)))
            .scalars()
            .all()
        )
        assert [p.tool_key for p in props] == ["scheduling.offer_partial"] and props[0].risk == "high"


# ================================================================ conflict at pick -> re-offer
async def test_pick_conflict_reoffers_next_round(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    with fake_llm(_script()) as fake:
        rid = await _start(k, fake, offer_ids[0], extra=lambda rid: [
            calls(("interviews.book", {"request_id": str(rid), "offer_id": str(offer_ids[0])})),
            calls(("scheduling.propose", {"request_id": str(rid)})),
            calls(("scheduling.offer_slots", {"offer_id": str(offer_ids[1]), "candidate_id": str(k.candidate_id), "channel": "email"})),
            wait(offer_ids[1]),
        ])  # fmt: skip
        first = (await offers_of(k, rid))[0]
        slot = first.slots[0]
        start = datetime.fromisoformat(slot["start"])
        k.fake.busy[k.admin.email.lower()] = [
            BusyBlock(start - timedelta(minutes=10), start + timedelta(minutes=50))
        ]
        token = await _token(k)
        subject = await svc.offers.link_subject(token)
        await svc.offers.pick(subject, 0, uuid.uuid4())
    req = await request_of(k, rid)
    assert req.status == "slots_offered" and req.round == 2 and req.booked_interview_id is None
    offers = await offers_of(k, rid)
    assert [o.status for o in offers] == ["superseded", "open"] and offers[1].round == 2
    assert all(datetime.fromisoformat(s["start"]) != start for s in offers[1].slots)
    kinds = await _history(k, rid)
    assert "conflict" in kinds and kinds.count("offered") == 2
    # the old link now shows the new options ("That time was just taken")
    view = await svc.offers.view(subject)
    assert view.status == "open" and view.notice == "slot_taken" and view.slots


# ================================================================ counter-proposals, max rounds
async def test_counter_window_that_matches_is_booked(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    when = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    with fake_llm(_script()) as fake:
        rid = await _start(k, fake, offer_ids[0], extra=lambda rid: [
            calls(("scheduling.check_times", {"request_id": str(rid)})),
            calls(("interviews.book", {"request_id": str(rid), "window": {"start": when.isoformat(), "end": (when + timedelta(hours=1)).isoformat()}})),
            done("booked"),
        ])  # fmt: skip
        subject = await svc.offers.link_subject(await _token(k))
        view = await svc.offers.counter(
            subject, [CounterWindowIn(start=when, end=when + timedelta(hours=2))], None
        )
        assert view.status in ("counter_proposed", "booked")
    req = await request_of(k, rid)
    assert req.status == "booked"
    async with tenant_session(k.tenant_id) as db:
        interview = await db.get(Interview, req.booked_interview_id)
        assert interview is not None and interview.starts_at == when
    runs = await runs_of(k)
    names = {c.tool_key for r in runs for c in await tool_calls(k, r.id)}
    assert "scheduling.check_times" in names  # resumed (live wait) or a continuation run (C-12)


async def test_counter_without_match_at_max_rounds_hands_off(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    resp = await k.admin.client.put(
        f"{API}/settings/agents/scheduling", json={"max_rounds": 1}, headers=csrf(k.admin.client)
    )
    assert resp.status_code == 200
    when = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    k.fake.busy[k.admin.email.lower()] = [BusyBlock(when - timedelta(hours=1), when + timedelta(hours=3))]
    with fake_llm(_script()) as fake:
        rid = await _start(k, fake, offer_ids[0], extra=lambda rid: [
            calls(("scheduling.check_times", {"request_id": str(rid)})),
            calls(("scheduling.propose", {"request_id": str(rid)})),
            done("handed_off"),
        ])  # fmt: skip
        subject = await svc.offers.link_subject(await _token(k))
        await svc.offers.counter(
            subject,
            [CounterWindowIn(start=when, end=when + timedelta(hours=2))],
            "Mornings are better for me",
        )
    req = await request_of(k, rid)
    assert req.status == "handed_off" and req.handoff_reason == "max_rounds"
    async with tenant_session(k.tenant_id) as db:
        tasks = (await db.execute(select(Task).where(Task.entity_id == k.application_id))).scalars().all()
        assert any(t.title.startswith("Scheduling needs you") for t in tasks)
    runs = await runs_of(k)
    assert runs[-1].tainted  # the candidate's note is untrusted data


async def test_counter_window_validation(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    with fake_llm(_script()) as fake:
        await _start(k, fake, offer_ids[0])
        subject = await svc.offers.link_subject(await _token(k))
        past = datetime.now(UTC) - timedelta(days=1)
        with pytest.raises(ValidationFailed):
            await svc.offers.counter(
                subject, [CounterWindowIn(start=past, end=past + timedelta(hours=2))], None
            )
        soon = datetime.now(UTC) + timedelta(days=2)
        with pytest.raises(ValidationFailed):
            await svc.offers.counter(
                subject, [CounterWindowIn(start=soon, end=soon + timedelta(minutes=20))], None
            )


# ================================================================ policy: Proposals
async def test_accept_conflicts_cancel_and_participant_changes_are_proposals(
    korg: KOrg, offer_ids: list[uuid.UUID]
) -> None:
    k = korg
    with fake_llm(_script()) as fake:
        rid = await _start(k, fake, offer_ids[0], extra=lambda rid: [
            calls(("interviews.book", {"request_id": str(rid), "offer_id": str(offer_ids[0]), "accept_conflicts": True})),
            done("needs_human"),
        ])  # fmt: skip
        subject = await svc.offers.link_subject(await _token(k))
        await svc.offers.pick(subject, 0, uuid.uuid4())
    runs = await runs_of(k)
    book = [c for c in await tool_calls(k, runs[0].id) if c.tool_key == "interviews.book"][0]
    assert book.outcome == "proposed" and book.risk == "high"
    assert (await request_of(k, rid)).status == "picked"
    # approve it: books as the approver
    async with tenant_session(k.tenant_id) as db:
        prop = (
            await db.execute(select(AgentProposal).where(AgentProposal.tool_key == "interviews.book"))
        ).scalar_one()
    resp = await k.admin.client.post(
        f"{API}/proposals/{prop.id}/decide",
        json={"decision": "approve"},
        headers={**csrf(k.admin.client), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    req = await request_of(k, rid)
    assert req.status == "booked" and req.booked_interview_id is not None
    # cancelling and changing interviewers are always Proposals
    with fake_llm({"agent_scheduling": [
        calls(("interviews.cancel", {"interview_id": str(req.booked_interview_id), "reason": "The candidate asked"})),
        calls(("interviews.change_participants", {"interview_id": str(req.booked_interview_id), "remove_user_ids": [str(k.admin.user_id)], "reason": "swap"})),
        done("needs_human"),
    ]}):  # fmt: skip
        async with tenant_session(k.tenant_id) as db:
            from app.services import agent_runtime_service as runtime

            ref = await runtime.start_run(
                k.ctx(),
                db,
                agent_key="scheduling",
                trigger_kind="manual",
                trigger_key="requested",
                subject=("application", k.application_id),
                owner_user_id=k.admin.user_id,
            )
    outcomes = {c.tool_key: c.outcome for c in await tool_calls(k, ref.run_id)}
    assert outcomes == {"interviews.cancel": "proposed", "interviews.change_participants": "proposed"}
    async with tenant_session(k.tenant_id) as db:
        interview = await db.get(Interview, req.booked_interview_id)
        assert interview is not None and interview.status == "scheduled"


# ================================================================ reschedule
async def test_interviewer_reschedule_bumps_sequence_same_uid(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    with fake_llm(_script()) as fake:
        rid = await _start(
            k,
            fake,
            offer_ids[0],
            extra=lambda rid: [
                calls(("interviews.book", {"request_id": str(rid), "offer_id": str(offer_ids[0])})),
                done("booked"),
            ],
        )
        await svc.offers.pick(await svc.offers.link_subject(await _token(k)), 0, uuid.uuid4())
        req = await request_of(k, rid)
        async with tenant_session(k.tenant_id) as db:
            before = await db.get(Interview, req.booked_interview_id)
            assert before is not None
            uid, seq = before.ical_uid, before.sequence
        resp = await k.admin.client.post(
            f"{API}/interviews/{req.booked_interview_id}/reschedule-requests",
            json={"reason": "I am travelling"},
            headers=csrf(k.admin.client),
        )
        assert resp.status_code == 201, resp.text
        rr = uuid.UUID(resp.json()["id"])
        assert resp.json()["kind"] == "reschedule"
    # the reschedule run was started with the next scripted turns: script it now and continue via a new start
    reschedule = await request_of(k, rr)
    assert reschedule.status == "open" and reschedule.interview_id == req.booked_interview_id
    with fake_llm(_script()) as fake:
        fake._by_prompt["agent_scheduling"].extend([
            calls(("scheduling.propose", {"request_id": str(rr)})),
            calls(("scheduling.offer_slots", {"offer_id": str(offer_ids[1]), "candidate_id": str(k.candidate_id), "channel": "email"})),
            wait(offer_ids[1]),
            calls(("interviews.reschedule", {"request_id": str(rr), "offer_id": str(offer_ids[1])})),
            done("booked"),
        ])  # fmt: skip
        async with tenant_session(k.tenant_id) as db:
            from app.services.scheduling_agent import requests as reqs
            from app.services.scheduling_agent.common import system_ctx

            row = await reqs.request_row(db, rr)
            await reqs.start_run(
                system_ctx(k.tenant_id),
                db,
                row,
                trigger_kind="manual",
                trigger_key="reschedule_requested",
                mode="reschedule",
            )
        offers = await offers_of(k, rr)
        assert offers and offers[-1].status == "open"
        async with tenant_session(k.tenant_id) as db:
            from app.core import secure_links

            link = await db.get(secure_links.SecureLink, offers[-1].secure_link_id)
            assert link is not None
        subject = svc.offers.portal_subject(k.tenant_id, k.candidate_id, offers[-1].id)
        await svc.offers.pick(subject, 0, uuid.uuid4())
    after_req = await request_of(k, rr)
    assert after_req.status == "booked"
    async with tenant_session(k.tenant_id) as db:
        after = await db.get(Interview, req.booked_interview_id)
        assert after is not None and after.ical_uid == uid and after.sequence == seq + 1
        assert after.starts_at == datetime.fromisoformat(offers[-1].slots[0]["start"])
    assert "rescheduled" in await _history(k, rr)


# ================================================================ closing
async def test_stage_exit_revokes_links_and_cancels(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    with fake_llm(_script()) as fake:
        rid = await _start(k, fake, offer_ids[0])
        token = await _token(k)
    from app.services import pipeline_service
    from tests.modules.interviews.helpers import admin_ctx

    async with tenant_session(k.tenant_id) as db:
        await pipeline_service.transition_application(
            admin_ctx(k.porg), db, k.application_id, "rejected", "user", reason_code="not_qualified"
        )
    req = await request_of(k, rid)
    assert req.status == "cancelled"
    gone = await k.admin.client.get(f"{API}/public/slot-pick/{token}")
    assert gone.status_code == 410


async def test_start_refusals(korg: KOrg) -> None:
    k = korg
    async with tenant_session(k.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await svc.requests.start(k.ctx(), db, k.application_id, RequestStartIn(stage_id=k.stage_id))
        assert exc.value.code == "participants_required"
        with pytest.raises(ConflictError) as exc2:
            await svc.requests.start(
                k.ctx(),
                db,
                k.application_id,
                RequestStartIn(stage_id=uuid.uuid4(), participant_user_ids=[k.admin.user_id]),
            )
        assert exc2.value.code == "stage_not_in_plan"
    from app.services import ai

    async with tenant_session(k.tenant_id) as db:
        await ai.set_features(db, k.tenant_id, {"agent_scheduling": False})
    resp = await k.admin.client.post(
        f"{API}/applications/{k.application_id}/scheduling-requests",
        json={"stage_id": str(k.stage_id), "participant_user_ids": [str(k.admin.user_id)]},
        headers={**csrf(k.admin.client), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "scheduling_agent_off"
