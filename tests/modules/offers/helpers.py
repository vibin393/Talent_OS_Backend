"""Shared helpers for the Epic 8 offer tests."""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.mailer import EmailMessage, get_mailer
from app.services import pipeline_service
from tests.modules.demands.helpers import DOrg, admin_ctx, h, open_demand
from tests.modules.identity.conftest import Member

OFFERS = "/api/v1/offers"
CANDIDATE_NAME = "Asha Rao"


def frontend() -> str:
    from app.core.settings import get_settings

    return get_settings().frontend_url.rstrip("/")


@dataclasses.dataclass
class Setup:
    org: DOrg
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID


async def create_candidate(m: Member, name: str = CANDIDATE_NAME, email: str | None = None) -> uuid.UUID:
    body = {"full_name": name, "emails": [{"email": email or f"cand-{uuid.uuid4().hex[:8]}@example.com"}]}
    resp = await m.client.post("/api/v1/candidates", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def add_application(m: Member, demand_id: uuid.UUID, candidate_id: uuid.UUID) -> uuid.UUID:
    resp = await m.client.post(
        f"/api/v1/demands/{demand_id}/applications",
        json={"candidate_ids": [str(candidate_id)], "source": "manual"},
        headers=h(m),
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "created", item
    return uuid.UUID(item["application_id"])


async def move(org: DOrg, application_id: uuid.UUID, *steps: tuple[str, str]) -> None:
    async with tenant_session(org.tenant_id) as db:
        for key, actor in steps:
            await pipeline_service.transition_application(admin_ctx(org), db, application_id, key, actor)  # type: ignore[arg-type]


TO_SELECTED = (
    ("shortlisted", "user"),
    ("submitted", "system"),
    ("interviewing", "user"),
    ("selected", "user"),
)


async def stage_key(org: DOrg, application_id: uuid.UUID) -> str:
    from app.models.pipeline import Application, PipelineStage

    async with tenant_session(org.tenant_id) as db:
        return str(
            (
                await db.execute(
                    select(PipelineStage.key)
                    .join(Application, Application.stage_id == PipelineStage.id)
                    .where(Application.id == application_id)
                )
            ).scalar_one()
        )


async def setup_selected(
    org: DOrg, *, name: str = CANDIDATE_NAME, email: str | None = None, **demand: Any
) -> Setup:
    d = await open_demand(org.admin, org.client_id, **{"positions": 1, **demand})
    demand_id = uuid.UUID(d["id"])
    candidate_id = await create_candidate(org.admin, name, email)
    application_id = await add_application(org.admin, demand_id, candidate_id)
    await move(org, application_id, *TO_SELECTED)
    return Setup(org=org, demand_id=demand_id, candidate_id=candidate_id, application_id=application_id)


async def create_offer(m: Member, application_id: uuid.UUID, **body: Any) -> dict[str, Any]:
    resp = await m.client.post(f"/api/v1/applications/{application_id}/offers", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def get_offer(m: Member, offer_id: str) -> dict[str, Any]:
    resp = await m.client.get(f"{OFFERS}/{offer_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def patch(m: Member, offer: dict[str, Any], **body: Any) -> Any:
    return await m.client.patch(
        f"{OFFERS}/{offer['id']}", json={"lock_version": offer["lock_version"], **body}, headers=h(m)
    )


async def submit(m: Member, offer_id: str) -> dict[str, Any]:
    resp = await m.client.post(f"{OFFERS}/{offer_id}/submit", headers=h(m))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def release(m: Member, offer_id: str) -> dict[str, Any]:
    resp = await m.client.post(f"{OFFERS}/{offer_id}/release", headers=h(m))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def released_offer(s: Setup, **body: Any) -> dict[str, Any]:
    """Create -> submit (no chain: auto-approved) -> release."""
    offer = await create_offer(s.org.admin, s.application_id, **body)
    approved = await submit(s.org.admin, offer["id"])
    assert approved["status"] == "approved", approved
    return await release(s.org.admin, offer["id"])


def emails(tenant_id: uuid.UUID, kind: str | None = None) -> list[EmailMessage]:
    sent = list(get_mailer().sent)  # type: ignore[attr-defined]
    return [m for m in sent if m.tenant_id == tenant_id and (kind is None or m.kind == kind)]


def link_token(message: EmailMessage) -> str:
    marker = f"{frontend()}/l/"
    start = message.text.index(marker) + len(marker)
    end = start
    while end < len(message.text) and not message.text[end].isspace():
        end += 1
    return message.text[start:end]


async def set_expires(org: DOrg, offer_id: str, at: datetime) -> None:
    from app.models.offers import Offer

    async with tenant_session(org.tenant_id) as db:
        await db.execute(update(Offer).where(Offer.id == uuid.UUID(offer_id)).values(expires_at=at))


def future(days: int = 7) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def utcnow() -> datetime:
    return datetime.now(UTC)
