# ruff: noqa: E501
"""Epic 33 isolation: cross-tenant 404, external principals 401, webhooks scoped to the config's tenant,
candidate portal Messages only for the signed-in candidate."""

import uuid

from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.comms import ChannelMessage
from app.services.messaging import opt_outs
from tests.epic10 import API, DOrg, h
from tests.modules.comms_p2.helpers import COMMS, PHONE, candidate, connector, enable, send, template, webhook
from tests.p2 import make_client_user, make_vendor_user, sign_in_candidate, sign_in_external


async def test_cross_tenant_404(dorg: DOrg, other_dorg: DOrg) -> None:
    m, o = dorg.admin, other_dorg.admin
    await enable(m)
    t = await template(m)
    cid = await candidate(m)
    assert (
        await o.client.patch(f"{COMMS}/channel-templates/{t['id']}", json={"name": "x"}, headers=h(o))
    ).status_code == 404
    assert (await o.client.get(f"{COMMS}/messaging-state/candidate/{cid}")).status_code == 404
    await enable(o)
    resp = await send(o, t["id"], cid)
    assert resp.status_code == 404
    add = await m.client.post(f"{COMMS}/opt-outs", json={"channel": "sms", "phone_e164": PHONE}, headers=h(m))
    assert (
        await o.client.post(f"{COMMS}/opt-outs/{add.json()['id']}/revoke", json={"reason": "x"}, headers=h(o))
    ).status_code == 404
    assert (
        await o.client.post(f"{COMMS}/manual-queue/{uuid.uuid4()}/mark-sent", headers=h(o))
    ).status_code == 404
    cfg = await connector(m)
    assert (
        await o.client.patch(f"{COMMS}/connectors/{cfg['id']}", json={"display_name": "x"}, headers=h(o))
    ).status_code == 404


async def test_external_principals_get_401(app: FastAPI, dorg: DOrg) -> None:
    cu = await make_client_user(dorg.tenant_id, dorg.client_id)
    vendor = uuid.uuid4()  # raw insert: independent of in-flight vendor model columns (Epic 17)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO vendors (id, tenant_id, human_id, legal_name, status) VALUES (:i, :t, :h, 'Vela', 'active')"
            ),
            {"i": vendor, "t": dorg.tenant_id, "h": f"VEN-{vendor.hex[:6]}"},
        )
    vu = await make_vendor_user(dorg.tenant_id, vendor)
    cid = await candidate(dorg.admin)
    clients = [
        (await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id)).client,
        (await sign_in_external(app, "vendor_user", vu, dorg.tenant_id, vendor)).client,
        await sign_in_candidate(app, cid, dorg.tenant_id),
    ]
    for client in clients:
        for path in (
            f"{COMMS}/channel-templates",
            f"{API}/connections/me/reply-capture",
            f"{API}/career-site/custom-domain",
        ):
            assert (await client.get(path)).status_code == 401, path
        await client.aclose()


async def test_portal_messages_only_own(app: FastAPI, dorg: DOrg) -> None:
    mine = await candidate(dorg.admin)
    other = await candidate(dorg.admin, phone="+919822223333", name="Other Person")
    client = await sign_in_candidate(app, mine, dorg.tenant_id)
    hdr = {"X-CSRF-Token": "csrf-token"}
    state = (await client.get(f"{API}/portal/messages")).json()
    assert state["has_phone"] is True and state["whatsapp_opted_in"] is False
    notice = state["whatsapp_notice"]["id"]
    on = await client.put(
        f"{API}/portal/messages/whatsapp", json={"enabled": True, "notice_id": notice}, headers=hdr
    )
    assert on.status_code == 200, on.text
    assert on.json()["whatsapp_opted_in"] is True
    off = await client.put(f"{API}/portal/messages/sms", json={"opted_out": True}, headers=hdr)
    assert off.status_code == 200 and off.json()["sms_opted_out"] is True
    await client.aclose()
    async with tenant_session(dorg.tenant_id) as db:
        assert await opt_outs.is_opted_out(db, "sms", PHONE)
        assert not await opt_outs.is_opted_out(db, "sms", "+919822223333")
        from app.services.messaging import sending

        assert not await sending.has_whatsapp_consent(db, "candidate", other)


async def test_webhook_cannot_touch_other_tenant(dorg: DOrg, other_dorg: DOrg) -> None:
    m, o = dorg.admin, other_dorg.admin
    await enable(o)
    await connector(o)
    t = await template(o)
    cid = await candidate(o)
    result = (await send(o, t["id"], cid)).json()["results"][0]
    async with tenant_session(other_dorg.tenant_id) as db:
        theirs = await db.get(ChannelMessage, uuid.UUID(result["message_id"]))
        assert theirs is not None
    mine = await connector(m)
    resp = await webhook(m, "sms", mine["id"], [
        {"kind": "delivery", "provider_message_id": theirs.provider_message_id, "status": "failed"},
        {"kind": "inbound", "from_e164": PHONE, "text": "STOP"},
    ])  # fmt: skip
    assert resp.status_code == 202
    async with tenant_session(other_dorg.tenant_id) as db:
        assert (await db.get(ChannelMessage, theirs.id)).status == "sent"  # type: ignore[union-attr]
        assert not await opt_outs.is_opted_out(db, "sms", PHONE)
    async with tenant_session(dorg.tenant_id) as db:
        assert await opt_outs.is_opted_out(db, "sms", PHONE)  # recorded in the config's own tenant only
        assert (
            len(
                list(
                    (
                        await db.execute(select(ChannelMessage).where(ChannelMessage.recipient_id == cid))
                    ).scalars()
                )
            )
            == 0
        )
