"""Sending, logging and bounces (story 10.1, FR-52). The mailbox provider (Epic 7) is faked."""

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core import mailer
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.services import comms_service
from app.tasks import comms as comms_tasks
from tests.epic10 import (
    API,
    EMAILS,
    DOrg,
    FakeMailbox,
    candidate,
    contact,
    count,
    h,
    member,
    open_demand,
    rows,
)


def _send_body(cand_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "to": [{"entity_type": "candidate", "entity_id": cand_id}],
        "subject": "Interview for {{ candidate.first_name }}",
        "body_html": "<p>Hi {{ candidate.first_name }}, regards {{ sender.name }}</p>",
    }
    body.update(overrides)
    return body


async def _message(tenant_id: uuid.UUID, message_id: str) -> dict[str, Any]:
    (row,) = await rows(tenant_id, "SELECT * FROM email_messages WHERE id = :id", id=uuid.UUID(message_id))
    return dict(row)


async def test_send_via_connected_microsoft_mailbox(
    dorg: DOrg, mailbox: FakeMailbox, outbox: mailer.OutboxMailer
) -> None:
    cand = await candidate(dorg.admin)
    demand = await open_demand(dorg.admin, dorg.client_id)
    resp = await dorg.admin.client.post(
        EMAILS,
        json=_send_body(cand["id"], related=[{"entity_type": "demand", "entity_id": demand["id"]}]),
        headers=h(dorg.admin),
    )
    assert resp.status_code == 202, resp.text
    (message_id,) = resp.json()["message_ids"]
    msg = await _message(dorg.tenant_id, message_id)
    assert (
        msg["status"] == "sent" and msg["transport"] == "mailbox_microsoft" and msg["fallback_reason"] is None
    )
    assert msg["subject"] == "Interview for Priya"
    assert len(mailbox.sent) == 1 and not outbox.sent
    assert mailbox.sent[0].headers["X-TOS-Message-Id"] == message_id
    # activity (no body) on the candidate and the related demand
    for entity_type, entity_id in (("candidate", cand["id"]), ("demand", demand["id"])):
        acts = await rows(
            dorg.tenant_id,
            "SELECT kind, body, data FROM activities WHERE entity_type = :t AND entity_id = :i AND kind = 'email'",  # noqa: E501
            t=entity_type,
            i=uuid.UUID(entity_id),
        )
        assert len(acts) == 1, entity_type
        assert "Interview for Priya" in acts[0]["body"] and "regards" not in acts[0]["body"]
        assert acts[0]["data"]["message_id"] == message_id
    timeline = await dorg.admin.client.get(f"{API}/candidates/{cand['id']}/timeline")
    assert any(a["kind"] == "email" for a in timeline.json()["items"])


async def test_expired_token_falls_back_to_platform_mailer(
    dorg: DOrg, mailbox: FakeMailbox, outbox: mailer.OutboxMailer
) -> None:
    mailbox.mode = "expired"
    cand = await candidate(dorg.admin)
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    assert resp.status_code == 202, resp.text
    msg = await _message(dorg.tenant_id, resp.json()["message_ids"][0])
    assert msg["transport"] == "platform" and msg["fallback_reason"] == "token_expired"
    assert msg["reply_to"] == dorg.admin.email.lower()
    (sent,) = [m for m in outbox.sent if m.headers.get("X-TOS-Message-Id") == str(msg["id"])]
    assert sent.reply_to == dorg.admin.email.lower()
    assert sent.from_name == f"Ada Admin via {dorg.tenant.name}"
    data = json.loads(outbox.outbox_path(sent).read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert data["reply_to"] == dorg.admin.email.lower()


async def test_no_mailbox_uses_platform(
    dorg: DOrg, mailbox: FakeMailbox, outbox: mailer.OutboxMailer
) -> None:
    mailbox.mode = "none"
    cand = await candidate(dorg.admin)
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    msg = await _message(dorg.tenant_id, resp.json()["message_ids"][0])
    assert msg["transport"] == "platform" and msg["fallback_reason"] == "no_mailbox" and len(outbox.sent) == 1


async def test_double_run_sends_once(dorg: DOrg, mailbox: FakeMailbox) -> None:
    cand = await candidate(dorg.admin)
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    message_id = resp.json()["message_ids"][0]
    assert comms_tasks.send_email(str(dorg.tenant_id), message_id) == "skipped"
    assert comms_tasks.send_email(str(dorg.tenant_id), message_id) == "skipped"
    assert len(mailbox.sent) == 1


async def test_transient_failures_retry_then_fail(dorg: DOrg, mailbox: FakeMailbox) -> None:
    mailbox.mode = "error"
    cand = await candidate(dorg.admin)
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    msg = await _message(dorg.tenant_id, resp.json()["message_ids"][0])
    assert msg["status"] == "failed" and msg["attempts"] == comms_service.MAX_ATTEMPTS
    # the sender gets an in-app "email_failed" notification
    assert (
        await count(
            dorg.tenant_id, "notifications", "type = 'email_failed' AND user_id = :u", u=dorg.admin.user_id
        )
        == 1
    )


async def test_recipient_without_email_is_422(dorg: DOrg, mailbox: FakeMailbox) -> None:
    cand = await candidate(dorg.admin, email=None)
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_email_missing"


async def test_one_message_per_recipient_when_recipient_fields_used(dorg: DOrg, mailbox: FakeMailbox) -> None:
    a = await candidate(dorg.admin, email="a.one@example.com", full_name="Alpha One")
    b = await candidate(dorg.admin, email="b.two@example.com", full_name="Beta Two")
    body = _send_body(a["id"], to=[{"entity_type": "candidate", "entity_id": c["id"]} for c in (a, b)])
    resp = await dorg.admin.client.post(EMAILS, json=body, headers=h(dorg.admin))
    assert len(resp.json()["message_ids"]) == 2
    assert sorted(m.subject for m in mailbox.sent) == ["Interview for Alpha", "Interview for Beta"]
    resp = await dorg.admin.client.post(
        EMAILS,
        json=_send_body(a["id"], to=body["to"], subject="Plain", body_html="<p>Plain</p>"),
        headers=h(dorg.admin),
    )
    assert len(resp.json()["message_ids"]) == 1


async def test_hard_bounced_address_warns_but_sends(dorg: DOrg, mailbox: FakeMailbox) -> None:
    cand = await candidate(dorg.admin, email="gone@example.com")
    async with tenant_session(dorg.tenant_id) as db:
        await comms_service.record_bounce(db, "gone@example.com", None, "hard", "mailbox unknown")
    status = await dorg.admin.client.get(f"{EMAILS}/address-status", params={"addresses": "gone@example.com"})
    assert status.status_code == 200 and status.json()[0]["hard_bounced_at"] is not None
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    assert resp.status_code == 202
    assert resp.json()["warnings"]["bounced_addresses"] == ["gone@example.com"]
    assert (await _message(dorg.tenant_id, resp.json()["message_ids"][0]))["status"] == "sent"
    assert len(mailbox.sent) == 1


async def test_marketing_opt_out_suppresses_only_marketing(dorg: DOrg, mailbox: FakeMailbox) -> None:
    cand = await candidate(dorg.admin, email="optout@example.com")
    async with tenant_session(dorg.tenant_id) as db:
        from datetime import UTC, datetime

        from app.models.comms import EmailAddressStatus

        db.add(
            EmailAddressStatus(
                tenant_id=dorg.tenant_id, address="optout@example.com", id=uuid.uuid4(),
                marketing_opt_out_at=datetime.now(UTC),
            )
        )  # fmt: skip
    resp = await dorg.admin.client.post(
        EMAILS, json=_send_body(cand["id"], category="marketing"), headers=h(dorg.admin)
    )
    assert resp.status_code == 202
    assert resp.json()["warnings"]["suppressed_addresses"] == ["optout@example.com"]
    msg = await _message(dorg.tenant_id, resp.json()["message_ids"][0])
    assert msg["status"] == "failed" and msg["error_code"] == "all_recipients_suppressed"
    (rec,) = await rows(
        dorg.tenant_id, "SELECT status FROM email_recipients WHERE message_id = :m", m=msg["id"]
    )
    assert rec["status"] == "suppressed" and not mailbox.sent

    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    assert (await _message(dorg.tenant_id, resp.json()["message_ids"][0]))["status"] == "sent"
    assert len(mailbox.sent) == 1


async def test_rate_limit(dorg: DOrg, mailbox: FakeMailbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "email_send_rate_per_user_hour", 2)
    cand = await candidate(dorg.admin)
    for _ in range(2):
        assert (
            await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
        ).status_code == 202
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"


async def test_contact_recipient_and_cc(dorg: DOrg, mailbox: FakeMailbox) -> None:
    person = await contact(dorg.admin, dorg.client_id)
    resp = await dorg.admin.client.post(
        EMAILS,
        json={
            "to": [{"entity_type": "contact", "entity_id": person["id"]}],
            "cc": [{"address": "boss@example.com"}],
            "subject": "Profiles for {{ client.name }}",
            "body_html": "<p>Dear {{ contact.first_name }}</p>",
            "context_refs": {"client_id": str(dorg.client_id)},
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 202, resp.text
    (sent,) = mailbox.sent
    assert sent.cc == ("boss@example.com",) and "Dear Rahul" in (sent.html or "")
    assert await count(dorg.tenant_id, "activities", "entity_type = 'contact' AND kind = 'email'") == 1


async def test_webhook_signature_and_bounce(
    app: FastAPI, dorg: DOrg, mailbox: FakeMailbox, webhook_secret: str, client: Any
) -> None:
    cand = await candidate(dorg.admin, email="bouncy@example.com")
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    message_id = resp.json()["message_ids"][0]
    payload = json.dumps(
        {"tenant_id": str(dorg.tenant_id), "address": "bouncy@example.com", "bounce_type": "hard",
         "reason": "550 no such user", "message_id": message_id}
    ).encode()  # fmt: skip
    url = f"{API}/public/webhooks/email-bounce"
    bad = await client.post(url, content=payload, headers={"X-TOS-Signature": "sha256=deadbeef"})
    assert bad.status_code == 401
    missing = await client.post(url, content=payload)
    assert missing.status_code == 401
    good = await client.post(
        url,
        content=payload,
        headers={"X-TOS-Signature": comms_service.signature_for(payload, webhook_secret)},
    )
    assert good.status_code == 200, good.text
    assert good.json()["matched"] is True
    (rec,) = await rows(
        dorg.tenant_id,
        "SELECT status, bounce_type FROM email_recipients WHERE message_id = :m",
        m=uuid.UUID(message_id),
    )
    assert rec["status"] == "bounced" and rec["bounce_type"] == "hard"
    (status,) = await rows(
        dorg.tenant_id,
        "SELECT hard_bounced_at FROM email_address_status WHERE address = 'bouncy@example.com'",
    )
    assert status["hard_bounced_at"] is not None
    assert (
        await count(
            dorg.tenant_id,
            "activities",
            "entity_id = :c AND kind = 'email' AND body LIKE '%bounced%'",
            c=uuid.UUID(cand["id"]),
        )
        == 1
    )
    assert (
        await count(
            dorg.tenant_id, "notifications", "type = 'email_bounced' AND user_id = :u", u=dorg.admin.user_id
        )
        == 1
    )


async def test_cli_bounce(dorg: DOrg, mailbox: FakeMailbox) -> None:
    cand = await candidate(dorg.admin, email="cli-bounce@example.com")
    await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    from app.cli import main

    assert main(["email-bounce", "cli-bounce@example.com", "--tenant-id", str(dorg.tenant_id)]) == 0
    assert await count(dorg.tenant_id, "email_recipients", "status = 'bounced'") == 1


async def test_email_reads_and_access(
    app: FastAPI, dorg: DOrg, other_dorg: DOrg, mailbox: FakeMailbox
) -> None:
    cand = await candidate(dorg.admin)
    resp = await dorg.admin.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(dorg.admin))
    message_id = resp.json()["message_ids"][0]
    detail = await dorg.admin.client.get(f"{EMAILS}/{message_id}")
    assert (
        detail.status_code == 200
        and detail.json()["status"] == "sent"
        and "Priya" in detail.json()["body_html"]
    )
    listed = await dorg.admin.client.get(f"{API}/candidates/{cand['id']}/emails")
    assert listed.status_code == 200 and [e["id"] for e in listed.json()["items"]] == [message_id]
    # an interviewer cannot read the candidate -> 404
    viewer = await member(app, dorg, "interviewer")
    assert (await viewer.client.get(f"{EMAILS}/{message_id}")).status_code == 404
    await viewer.client.aclose()
    # cross-tenant
    assert (await other_dorg.admin.client.get(f"{EMAILS}/{message_id}")).status_code == 404
    assert (await other_dorg.admin.client.get(f"{API}/candidates/{cand['id']}/emails")).status_code == 404
    resp = await other_dorg.admin.client.post(
        EMAILS, json=_send_body(cand["id"]), headers=h(other_dorg.admin)
    )
    assert resp.status_code == 404


async def test_send_requires_permission(app: FastAPI, dorg: DOrg) -> None:
    cand = await candidate(dorg.admin)
    viewer = await member(app, dorg, "viewer")
    resp = await viewer.client.post(EMAILS, json=_send_body(cand["id"]), headers=h(viewer))
    assert resp.status_code == 403
    await viewer.client.aclose()


async def test_render_template_api_for_other_epics(dorg: DOrg) -> None:
    cand = await candidate(dorg.admin)
    demand = await open_demand(dorg.admin, dorg.client_id)
    from tests.modules.demands.helpers import admin_ctx

    async with tenant_session(dorg.tenant_id) as db:
        rendered = await comms_service.render_template(
            admin_ctx(dorg), db, "rejection", "en", {"candidate_id": cand["id"], "demand_id": demand["id"]}
        )
    assert rendered is not None
    assert rendered.subject == "Your application for Senior Data Engineer"
    assert "Hi Priya" in rendered.body_html
