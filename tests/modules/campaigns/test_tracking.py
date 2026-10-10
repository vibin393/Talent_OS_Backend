# ruff: noqa: E501
"""Epic 39 story 39.4 (FR-198): signed links, pixel, clicks, unsubscribe, complaints, reputation, attribution."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.campaigns import CampaignAttribution, EngagementEvent, MarketingSettings
from app.models.comms import EmailAddressStatus
from app.models.pipeline import Application
from app.services.campaigns import attribution, engagement, reputation, tokens
from tests.epic5 import h
from tests.modules.campaigns.helpers import (
    API,
    audience,
    candidate,
    email_campaign,
    insert_message,
    messages,
    now,
    setup_marketing,
    submit,
    token_for,
)
from tests.modules.demands.helpers import DOrg

PUBLIC = "/api/v1/public/m"


async def _sent_campaign(dorg: DOrg, *cands: uuid.UUID, **body: Any) -> dict[str, Any]:
    m = dorg.admin
    await setup_marketing(m)
    aud = await audience(m, list(cands))
    camp = await email_campaign(m, aud["id"], **body)
    resp = await submit(m, camp["id"])
    assert resp.status_code == 200, resp.text
    return camp


async def _events(tenant_id: uuid.UUID, kind: str) -> list[EngagementEvent]:
    async with tenant_session(tenant_id) as db:
        return list((await db.execute(select(EngagementEvent).where(EngagementEvent.kind == kind))).scalars())


async def test_pixel_only_records_messages_rendered_with_it_and_bad_tokens_404(dorg: DOrg, other_dorg: DOrg) -> None:
    m = dorg.admin
    tracked = await candidate(m, "Tara", consents=("marketing", "tracking"))
    plain = await candidate(m, "Uma")
    camp = await _sent_campaign(dorg, tracked, plain)
    rows = {r.candidate_id: r for r in await messages(dorg.tenant_id, campaign_id=camp["id"])}
    for _ in range(2):
        r = await m.client.get(f"{PUBLIC}/{token_for(rows[tracked])}/o.gif")
        assert r.status_code == 200 and r.headers["content-type"] == "image/gif"
    await m.client.get(f"{PUBLIC}/{token_for(rows[plain])}/o.gif")
    opened = await _events(dorg.tenant_id, "opened")
    assert [e.candidate_id for e in opened] == [tracked]  # once, and only with the pixel
    # forged MAC / other tenant -> 404
    good = token_for(rows[plain])
    forged = good[:-1] + ("a" if good[-1] != "a" else "b")
    assert (await m.client.get(f"{PUBLIC}/{forged}/u")).status_code == 404
    cross = tokens.make(other_dorg.tenant_id, rows[plain].id, rows[plain].token_nonce)
    assert (await m.client.get(f"{PUBLIC}/{cross}/u")).status_code == 404
    assert (await m.client.get(f"{PUBLIC}/not-a-real-token-at-all-xxxxx/u")).status_code == 404


async def test_click_redirects_only_to_stored_links_and_resolves_the_attribution_token(dorg: DOrg) -> None:
    from app.models.campaigns import Campaign

    m = dorg.admin
    cid = await candidate(m, "Clicky")
    await setup_marketing(m)
    aud = await audience(m, [cid])
    camp = await email_campaign(m, aud["id"], email_body_html='<p>See <a href="{{job_link.1}}">the role</a></p>')
    url = "https://app.example.test/careers/initech/jobs/00000000-0000-0000-0000-000000000001/data-engineer"
    async with tenant_session(dorg.tenant_id) as db:
        c = await db.get(Campaign, uuid.UUID(camp["id"]))
        assert c is not None
        c.job_links = [{"index": 0, "posting_id": str(uuid.uuid4()), "demand_id": None, "label": "Data engineer", "url": url}]
    assert (await submit(m, camp["id"])).status_code == 200
    (row,) = await messages(dorg.tenant_id, campaign_id=camp["id"])
    tok = token_for(row)
    assert (await m.client.get(f"{PUBLIC}/{tok}/c/7", follow_redirects=False)).status_code == 404
    r = await m.client.get(f"{PUBLIC}/{tok}/c/0", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{url}?tos_ct={tok}"
    assert len(await _events(dorg.tenant_id, "clicked")) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await attribution.resolve_attribution_token(db, dorg.tenant_id, tok) == str(row.id)
        assert await attribution.resolve_attribution_token(db, uuid.uuid4(), tok) is None
        assert await attribution.resolve_attribution_token(db, dorg.tenant_id, tok[:-2] + "zz") is None


async def test_one_click_unsubscribe_and_all_marketing_withdrawal(dorg: DOrg) -> None:
    from app.services.privacy import consent

    m = dorg.admin
    a = await candidate(m, "Anu", email="anu@example.com")
    b = await candidate(m, "Bea")
    camp = await _sent_campaign(dorg, a, b)
    rows = {r.candidate_id: r for r in await messages(dorg.tenant_id, campaign_id=camp["id"])}
    view = await m.client.get(f"{PUBLIC}/{token_for(rows[a])}/u")
    assert view.status_code == 200 and view.json() == {"tenant_name": "Initech Talent", "channel": "email", "already_unsubscribed": False}
    # a later campaign queues a pending message for a
    later = await insert_message(dorg.tenant_id, a, campaign_id=uuid.UUID((await email_campaign(m, (await audience(m, [a]))["id"]))["id"]), status="pending", sent_at=None)
    one_click = await m.client.post(
        f"{PUBLIC}/{token_for(rows[a])}/u", content=b"List-Unsubscribe=One-Click", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert one_click.status_code == 200, one_click.text
    assert one_click.json() == {"status": "unsubscribed", "channel": "email", "all": False}
    async with tenant_session(dorg.tenant_id) as db:
        status = await db.get(EmailAddressStatus, (dorg.tenant_id, "anu@example.com"))
        assert status is not None and status.marketing_opt_out_at is not None
    (pending,) = await messages(dorg.tenant_id, id=later)
    assert pending.status == "suppressed" and pending.suppression_reason == "channel_opt_out"
    assert (await m.client.get(f"{PUBLIC}/{token_for(rows[a])}/u")).json()["already_unsubscribed"] is True
    all_out = await m.client.post(f"{PUBLIC}/{token_for(rows[b])}/u", json={"all": True})
    assert all_out.json()["all"] is True
    async with tenant_session(dorg.tenant_id) as db:
        assert not await consent.has_active_consent(db, b, "marketing")
    assert len(await _events(dorg.tenant_id, "unsubscribed")) == 2


async def test_complaint_adds_opt_out_and_reputation_breach_pauses_the_tenant(dorg: DOrg, monkeypatch: Any) -> None:
    from app.models.campaigns import Campaign

    m = dorg.admin
    cid = await candidate(m, "Spam", email="spam@example.com")
    camp = await _sent_campaign(dorg, cid)
    (row,) = await messages(dorg.tenant_id, campaign_id=camp["id"])
    async with tenant_session(dorg.tenant_id) as db:
        assert await engagement.on_email_bounced(db, row.email_message_id, "complaint")
    async with tenant_session(dorg.tenant_id) as db:
        status = await db.get(EmailAddressStatus, (dorg.tenant_id, "spam@example.com"))
        assert status is not None and status.marketing_opt_out_at is not None
    assert len(await _events(dorg.tenant_id, "complained")) == 1
    # a second campaign is sending when the guard runs (1 complaint / 1 delivered with the minimum lowered)
    other = await candidate(m, "Other")
    camp2 = uuid.UUID((await email_campaign(m, (await audience(m, [other]))["id"], name="Big"))["id"])
    async with tenant_session(dorg.tenant_id) as db:
        c2 = await db.get(Campaign, camp2)
        assert c2 is not None
        c2.status = "sending"
    monkeypatch.setattr(reputation, "MIN_DENOMINATOR", 1)
    async with tenant_session(dorg.tenant_id) as db:
        assert await reputation.check(db) == "complaint_rate"
    async with tenant_session(dorg.tenant_id) as db:
        settings = await db.get(MarketingSettings, dorg.tenant_id)
        assert settings is not None and settings.paused_reason == "complaint_rate"
        c2 = await db.get(Campaign, camp2)
        assert c2 is not None and c2.status == "paused"
    blocked = await m.client.post(f"{API}/campaigns/{camp2}/resume", headers=h(m))
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "marketing_paused"
    resumed = await m.client.post(f"{API}/settings/resume", json={"reason": "Lists cleaned"}, headers=h(m))
    assert resumed.status_code == 200 and resumed.json()["paused_at"] is None
    assert (await m.client.post(f"{API}/campaigns/{camp2}/resume", headers=h(m))).json()["status"] in ("scheduled", "sending", "completed")


async def _application(dorg: DOrg, cid: uuid.UUID) -> uuid.UUID:
    from tests.modules.demands.helpers import open_demand

    m = dorg.admin
    demand = await open_demand(m, dorg.client_id)
    resp = await m.client.post(f"/api/v1/demands/{demand['id']}/applications", json={"candidate_ids": [str(cid)]}, headers=h(m))
    assert resp.status_code in (200, 201), resp.text
    async with tenant_session(dorg.tenant_id) as db:
        return (await db.execute(select(Application.id).where(Application.candidate_id == cid))).scalar_one()


async def test_last_touch_attribution_within_thirty_days(dorg: DOrg) -> None:
    m = dorg.admin
    near = await candidate(m, "Near")
    far = await candidate(m, "Far")
    aud = await audience(m, [near, far])
    old_camp = uuid.UUID((await email_campaign(m, aud["id"], name="Old"))["id"])
    new_camp = uuid.UUID((await email_campaign(m, aud["id"], name="New"))["id"])
    # near: clicked 29 days ago (old) and 10 days ago (new) -> last touch = new
    for camp_id, days in ((old_camp, 29), (new_camp, 10)):
        mid = await insert_message(dorg.tenant_id, near, campaign_id=camp_id, sent_at=now() - timedelta(days=days))
        async with tenant_session(dorg.tenant_id) as db:
            from app.models.campaigns import CampaignMessage

            cm = await db.get(CampaignMessage, mid)
            assert cm is not None
            await engagement.record(db, cm, "clicked", at=now() - timedelta(days=days))
    # far: touched 31 days ago only
    await insert_message(dorg.tenant_id, far, campaign_id=old_camp, sent_at=now() - timedelta(days=31))
    app_near = await _application(dorg, near)
    app_far = await _application(dorg, far)
    async with tenant_session(dorg.tenant_id) as db:
        rows = {a.application_id: a for a in (await db.execute(select(CampaignAttribution))).scalars()}
    assert app_near in rows and rows[app_near].campaign_id == new_camp
    assert app_far not in rows
    assert len(await _events(dorg.tenant_id, "applied")) == 1
    analytics = (await m.client.get(f"{API}/campaigns/{new_camp}/analytics")).json()
    assert analytics["counts"]["applied"] == 1 and analytics["counts"]["clicked"] == 1
