# ruff: noqa: E501, ANN001
"""Epic 59 A6: settings and preferences API, request reads, the public slot-pick page (expiry, timezone, invalid
tokens, rate limit), cross-tenant isolation, erasure, the P1 reminder-owner registry contract."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core import rate_limit
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.scheduling_agent import SchedulingSlotOffer
from app.services import scheduling_agent_service as svc
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.scheduling.conftest import API, KOrg, offer_script, offers_of, request_of
from tests.modules.identity.conftest import csrf

BENIGN = FakeTurn("emit_result", result={"verdict": "benign", "reasons": []})


async def _offered(k: KOrg, offer_ids: list[uuid.UUID]) -> tuple[uuid.UUID, str]:
    import re

    from app.models.comms import Email
    from app.schemas.scheduling_agent import RequestStartIn

    with fake_llm({"agent_scheduling": [], None: [BENIGN] * 5}) as fake:
        async with tenant_session(k.tenant_id) as db:
            req = await svc.requests.start(
                k.ctx(),
                db,
                k.application_id,
                RequestStartIn(stage_id=k.stage_id, participant_user_ids=[k.admin.user_id]),
            )
            rid = req.id
            fake._by_prompt["agent_scheduling"].extend(offer_script(k, rid, offer_ids[0]))
    async with tenant_session(k.tenant_id) as db:
        body = (
            await db.execute(select(Email.body_html).order_by(Email.created_at.desc()).limit(1))
        ).scalar_one()
    token = re.search(r"/l/([A-Za-z0-9_\-]{20,})", body or "")
    assert token is not None
    return rid, token.group(1)


async def test_settings_and_preferences_api(korg: KOrg) -> None:
    k = korg
    c = k.admin.client
    got = await c.get(f"{API}/settings/agents/scheduling")
    assert got.status_code == 200 and got.json()["max_rounds"] == 3 and got.json()["agent_on"] is True
    bad = await c.put(f"{API}/settings/agents/scheduling", json={"slot_step_minutes": 20}, headers=csrf(c))
    assert bad.status_code == 422
    ok = await c.put(
        f"{API}/settings/agents/scheduling",
        json={"reminder_offsets_minutes": [60, 1440, 60], "max_slots": 4},
        headers=csrf(c),
    )
    assert (
        ok.status_code == 200
        and ok.json()["reminder_offsets_minutes"] == [1440, 60]
        and ok.json()["max_slots"] == 4
    )
    prefs = await c.get(f"{API}/me/scheduling-preferences")
    assert (
        prefs.status_code == 200
        and prefs.json()["timezone"] == "Asia/Kolkata"
        and prefs.json()["calendar_connected"] is True
    )
    bad_tz = await c.put(
        f"{API}/me/scheduling-preferences",
        json={"timezone": "Mars/Base", "working_hours": {"mon": [["09:00", "17:00"]]}},
        headers=csrf(c),
    )
    assert bad_tz.status_code == 422
    bad_hours = await c.put(
        f"{API}/me/scheduling-preferences",
        json={"timezone": "Asia/Kolkata", "working_hours": {"mon": [["17:00", "09:00"]]}},
        headers=csrf(c),
    )
    assert bad_hours.status_code == 422
    other = await c.get(f"{API}/users/{k.admin.user_id}/scheduling-preferences")
    assert other.status_code == 200
    opts = await c.get(
        f"{API}/applications/{k.application_id}/scheduling-options", params={"stage_id": str(k.stage_id)}
    )
    assert (
        opts.status_code == 200
        and opts.json()["candidate_timezone"] == "Asia/Kolkata"
        and opts.json()["timezone_source"] == "location"
    )


async def test_public_page_timezone_expiry_and_invalid(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    rid, token = await _offered(k, offer_ids)
    c = k.admin.client
    tz = await c.post(f"{API}/public/slot-pick/{token}/timezone", json={"tz": "Europe/London"})
    assert tz.status_code == 200 and tz.json()["candidate_timezone"] == "Europe/London"
    assert (await request_of(k, rid)).candidate_timezone == "Europe/London"
    bad = await c.post(f"{API}/public/slot-pick/{token}/timezone", json={"tz": "Nowhere/Zone"})
    assert bad.status_code == 422
    out_of_range = await c.post(
        f"{API}/public/slot-pick/{token}/pick", json={"index": 9, "pick_id": str(uuid.uuid4())}
    )
    assert out_of_range.status_code == 422
    assert (await c.get(f"{API}/public/slot-pick/{'x' * 40}")).status_code == 404
    # the offer expires -> the page says so and a pick is refused; the expiry job closes the request
    async with tenant_session(k.tenant_id) as db:
        await db.execute(
            update(SchedulingSlotOffer)
            .where(SchedulingSlotOffer.request_id == rid)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    pick = await c.post(
        f"{API}/public/slot-pick/{token}/pick", json={"index": 0, "pick_id": str(uuid.uuid4())}
    )
    assert pick.status_code == 409
    async with tenant_session(k.tenant_id) as db:
        assert await svc.requests.expire_due(db) == 1
    assert (await request_of(k, rid)).status == "expired"
    assert (await c.get(f"{API}/public/slot-pick/{token}")).status_code == 410


async def test_public_rate_limit(
    korg: KOrg, offer_ids: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    k = korg
    _, token = await _offered(k, offer_ids)
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    rate_limit.set_backend(rate_limit.memory_backend())
    rate_limit.memory_backend().reset()
    from app.router import public_scheduling

    monkeypatch.setattr(public_scheduling, "IP_LIMIT_PER_HOUR", 3)
    codes = [(await k.admin.client.get(f"{API}/public/slot-pick/{token}")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


async def test_cross_tenant_isolation(
    app: FastAPI, korg: KOrg, offer_ids: list[uuid.UUID], _connections_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.agents.scheduling.conftest import make_korg

    k = korg
    rid, _ = await _offered(k, offer_ids)
    other = await make_korg(app, _connections_env, monkeypatch)
    try:
        oc = other.admin.client
        assert (await oc.get(f"{API}/scheduling-requests/{rid}")).status_code == 404
        assert (await oc.get(f"{API}/applications/{k.application_id}/scheduling-requests")).status_code == 404
        offer = (await offers_of(k, rid))[0]
        subject = svc.offers.portal_subject(other.tenant_id, other.candidate_id, offer.id)
        from app.core.errors import NotFound

        with pytest.raises(NotFound):
            await svc.offers.view(subject, portal=True)
        # the same tenant but another candidate is not found either
        with pytest.raises(NotFound):
            await svc.offers.view(svc.offers.portal_subject(k.tenant_id, uuid.uuid4(), offer.id), portal=True)
        mine = await svc.offers.portal_list(k.tenant_id, k.candidate_id)
        assert [v.offer_id for v in mine] == [offer.id]
    finally:
        await other.admin.client.aclose()


async def test_request_detail_and_cancel(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    k = korg
    rid, token = await _offered(k, offer_ids)
    c = k.admin.client
    listing = await c.get(f"{API}/applications/{k.application_id}/scheduling-requests")
    assert (
        listing.status_code == 200
        and listing.json()[0]["id"] == str(rid)
        and listing.json()[0]["status"] == "slots_offered"
    )
    detail = (await c.get(f"{API}/scheduling-requests/{rid}")).json()
    assert detail["round"] == 1 and detail["max_rounds"] == 3 and detail["offers"][0]["status"] == "open"
    assert [h["kind"] for h in detail["history"]][:3] == ["requested", "slots_computed", "offered"]
    resp = await c.post(f"{API}/scheduling-requests/{rid}/cancel", json={"reason": "filled"}, headers=csrf(c))
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert (await c.get(f"{API}/public/slot-pick/{token}")).status_code == 410
    again = await c.post(f"{API}/scheduling-requests/{rid}/cancel", json={}, headers=csrf(c))
    assert again.status_code == 409


async def test_erasure_nulls_candidate_text(korg: KOrg, offer_ids: list[uuid.UUID]) -> None:
    from app.schemas.scheduling_public import CounterWindowIn
    from app.services.privacy.registry import ErasureContext
    from app.services.scheduling_agent_erasure import SchedulingRequestsHandler

    k = korg
    rid, token = await _offered(k, offer_ids)
    when = datetime.now(UTC) + timedelta(days=3)
    with fake_llm(
        {
            "agent_scheduling": [FakeTurn("emit_result", result={"outcome": "waiting", "summary": "x"})],
            None: [BENIGN] * 5,
        }
    ):
        await svc.offers.counter(
            await svc.offers.link_subject(token),
            [CounterWindowIn(start=when, end=when + timedelta(hours=2))],
            "Only evenings, my phone is 98450 12345",
        )
    handler = SchedulingRequestsHandler()
    async with tenant_session(k.tenant_id) as db:
        ctx = ErasureContext(tenant_id=k.tenant_id, candidate_id=k.candidate_id, run_id=uuid.uuid4(), reason="dsr")
        exported = await handler.export(db, ctx)
        assert exported and exported["scheduling_requests"][0]["offers"][0]["note"]
        await handler.erase(db, ctx)
        assert await handler.verify(db, ctx) == []
    offer = (await offers_of(k, rid))[0]
    assert offer.candidate_note is None and offer.counter_windows is None


def test_reminder_owner_registry_contract() -> None:
    from app.services.interviews import invites
    from app.services.scheduling_agent.reminders import owns_reminders

    assert owns_reminders in invites._REMINDER_OWNERS
    before = list(invites._REMINDER_OWNERS)
    invites.register_reminder_owner(owns_reminders)  # idempotent
    assert before == invites._REMINDER_OWNERS
    assert timedelta(minutes=10) == invites.OWNER_GRACE
