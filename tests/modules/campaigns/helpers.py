# ruff: noqa: E501, D401
"""Epic 39 test helpers (audiences, campaigns, sequences)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.campaigns import CampaignMessage
from app.services.campaigns import tokens
from tests.epic5 import h, make_candidate
from tests.modules.identity.conftest import Member

API = "/api/v1/marketing"
FOOTER = "Initech Talent, 1 MG Road, Bengaluru 560001"


def now() -> datetime:
    return datetime.now(UTC)


async def candidate(
    m: Member,
    name: str = "Asha Rao",
    *,
    email: str | None = "auto",
    phone: str | None = None,
    consents: tuple[str, ...] = ("marketing",),
    country: str | None = None,
) -> uuid.UUID:
    body: dict[str, Any] = {}
    if email:
        body["emails"] = [
            {
                "email": f"{uuid.uuid4().hex[:10]}@example.com" if email == "auto" else email,
                "is_primary": True,
            }
        ]
    if phone:
        body["phones"] = [{"phone": phone, "is_primary": True}]
    if country:
        body["location"] = {"city": "X", "country": country}
    cid = uuid.UUID((await make_candidate(m, name, **body))["id"])
    for purpose in consents:
        await grant(m.tenant_id, cid, purpose)
    return cid


async def grant(tenant_id: uuid.UUID, candidate_id: uuid.UUID, purpose: str) -> None:
    from app.services.privacy import consent

    async with tenant_session(tenant_id) as db:
        await consent.grant_consent(
            RequestContext(tenant_id=tenant_id), db, candidate_id, purpose, source="recruiter_attestation"
        )


async def withdraw(tenant_id: uuid.UUID, candidate_id: uuid.UUID, purpose: str) -> None:
    from app.services.privacy import consent

    async with tenant_session(tenant_id) as db:
        await consent.withdraw_consent(RequestContext(tenant_id=tenant_id), db, candidate_id, purpose)


async def setup_marketing(m: Member, **extra: Any) -> dict[str, Any]:
    resp = await m.client.patch(f"{API}/settings", json={"footer_contact": FOOTER, **extra}, headers=h(m))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def audience(
    m: Member, include: list[uuid.UUID], exclude: list[uuid.UUID] | None = None, **body: Any
) -> dict[str, Any]:
    resp = await m.client.post(f"{API}/audiences", json={"name": "Audience", **body}, headers=h(m))
    assert resp.status_code == 201, resp.text
    aud = dict(resp.json())
    if include:
        r = await m.client.post(
            f"{API}/audiences/{aud['id']}/list-entries",
            json={"mode": "include", "candidate_ids": [str(c) for c in include]},
            headers=h(m),
        )
        assert r.status_code == 200, r.text
    if exclude:
        r = await m.client.post(
            f"{API}/audiences/{aud['id']}/list-entries",
            json={"mode": "exclude", "candidate_ids": [str(c) for c in exclude]},
            headers=h(m),
        )
        assert r.status_code == 200, r.text
    return aud


async def email_campaign(m: Member, audience_id: str, **body: Any) -> dict[str, Any]:
    payload = {
        "name": "Spring hiring",
        "channel": "email",
        "audience_id": audience_id,
        "email_subject": "Hello {{candidate.first_name}}",
        "email_body_html": "<p>We have new roles for you.</p>",
        **body,
    }
    resp = await m.client.post(f"{API}/campaigns", json=payload, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def submit(m: Member, campaign_id: str, **body: Any) -> Any:
    return await m.client.post(f"{API}/campaigns/{campaign_id}/submit", json=body, headers=h(m))


async def messages(tenant_id: uuid.UUID, **where: Any) -> list[CampaignMessage]:
    async with tenant_session(tenant_id) as db:
        stmt = select(CampaignMessage)
        for key, value in where.items():
            stmt = stmt.where(
                getattr(CampaignMessage, key)
                == (uuid.UUID(str(value)) if key.endswith("_id") and value else value)
            )
        return list((await db.execute(stmt.order_by(CampaignMessage.created_at))).scalars())


def token_for(cm: CampaignMessage) -> str:
    return tokens.make(cm.tenant_id, cm.id, cm.token_nonce)


async def insert_message(
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    campaign_id: uuid.UUID | None = None,
    status: str = "sent",
    sent_at: datetime | None = None,
    channel: str = "email",
    enrollment_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A historical marketing message (for caps / attribution) without delivering anything."""
    async with tenant_session(tenant_id) as db:
        cm = CampaignMessage(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            enrollment_id=enrollment_id,
            step_id=step_id,
            candidate_id=candidate_id,
            channel=channel,
            status=status,
            sent_at=sent_at if sent_at is not None else now() - timedelta(days=1),
            token_nonce=tokens.new_nonce(),
        )
        db.add(cm)
    return cm.id


async def bare_campaign(m: Member, audience_id: str) -> uuid.UUID:
    """A draft campaign row (owner of historical messages)."""
    return uuid.UUID((await email_campaign(m, audience_id))["id"])
