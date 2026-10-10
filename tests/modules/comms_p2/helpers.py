"""Helpers for the Epic 33 tests (fake SMS / WhatsApp connectors; never a real provider call)."""

import json
import uuid
from typing import Any

from app.services.connectors.sms.fake import fake_signature
from tests.epic10 import API, DOrg, h
from tests.modules.identity.conftest import Member

COMMS = f"{API}/comms"
SECRET = "whsec-epic33-secret"
PHONE = "+919876543210"
DLT_ID = "1207161234567890123"


async def enable(m: Member, **extra: Any) -> None:
    body = {
        "sms_enabled": True,
        "whatsapp_enabled": True,
        "quiet_hours": {"start": "00:00", "end": "00:00", "tz": "Asia/Kolkata"},
        **extra,
    }
    resp = await m.client.patch(f"{COMMS}/channel-settings", json=body, headers=h(m))
    assert resp.status_code == 200, resp.text


async def connector(m: Member, category: str = "sms", provider: str = "fake") -> dict[str, Any]:
    resp = await m.client.post(
        f"{COMMS}/connectors",
        json={
            "category": category,
            "provider_key": provider,
            "display_name": f"{category} {provider}",
            "webhook_secret": SECRET,
        },
        headers=h(m),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def template(m: Member, channel: str = "sms", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "channel": channel,
        "key": f"t-{uuid.uuid4().hex[:6]}",
        "name": "Timesheet reminder",
        "purpose": "timesheet_reminder",
        "body": "Hi {#var#}, please submit your timesheet."
        if channel == "sms"
        else "Hi {{1}}, please submit your timesheet.",
        "variables": ["candidate.first_name"],
        "provider_template_ref": DLT_ID if channel == "sms" else "timesheet_reminder",
        "sender_id": "TALOS1" if channel == "sms" else None,
        "status": "registered",
        **extra,
    }
    resp = await m.client.post(f"{COMMS}/channel-templates", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def candidate(m: Member, phone: str | None = PHONE, name: str = "Priya Sharma") -> uuid.UUID:
    body: dict[str, Any] = {
        "full_name": name,
        "emails": [{"email": f"{uuid.uuid4().hex[:8]}@example.com", "is_primary": True}],
    }
    if phone:
        body["phones"] = [{"phone": phone, "is_primary": True}]
    resp = await m.client.post(f"{API}/candidates", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def send(
    m: Member, template_id: str, *candidate_ids: uuid.UUID, channel: str = "sms", key: str | None = None
) -> Any:
    headers = h(m) | ({"Idempotency-Key": key} if key else {})
    return await m.client.post(
        f"{COMMS}/messages/send",
        json={
            "channel": channel,
            "template_id": template_id,
            "recipients": [{"entity_type": "candidate", "entity_id": str(c)} for c in candidate_ids],
        },
        headers=headers,
    )


async def webhook(
    m: Member, category: str, config_id: str, events: list[dict[str, Any]], *, sign: bool = True
) -> Any:
    body = json.dumps({"events": events}).encode()
    headers = {
        "X-Fake-Signature": fake_signature(SECRET, body) if sign else "bad",
        "Content-Type": "application/json",
    }
    return await m.client.post(f"{API}/hooks/{category}/{config_id}", content=body, headers=headers)


async def attest(org: DOrg, candidate_id: uuid.UUID) -> None:
    m = org.admin
    state = await m.client.get(f"{COMMS}/messaging-state/candidate/{candidate_id}")
    assert state.status_code == 200, state.text
    notice = state.json()["whatsapp_notice"]
    resp = await m.client.post(
        f"{COMMS}/whatsapp-attestation",
        json={"candidate_id": str(candidate_id), "notice_id": notice["id"]},
        headers=h(m),
    )
    assert resp.status_code == 200 and resp.json()["whatsapp_consent"] is True, resp.text
