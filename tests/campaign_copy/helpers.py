# ruff: noqa: E501
"""Epic 78 test helpers: a draft email campaign with a seeded audience, Fake LLM scripts and service calls."""

import dataclasses
import uuid
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.models.campaign_copy import CampaignCopyDraft, CampaignCopyRequest
from app.schemas.campaign_copy import CopyRequestIn
from app.services import ai, campaign_copy_service
from app.services.ai.fake_llm import FakeLLM, FakeTurn, fake_llm
from tests.modules.campaigns.helpers import audience, candidate, email_campaign, setup_marketing
from tests.modules.demands.helpers import DOrg

READ_TOOLS = ("campaign_copy.get_brief", "campaign_copy.get_postings", "campaign_copy.get_templates", "campaign_copy.get_tenant_style")
GOOD_EMAIL = {
    "subject": "New data roles for you, {{candidate.first_name}}",
    "preheader": "Fresh openings picked for your skills",
    "body_html": "<p>Hi {{candidate.first_name}},</p><p>We have new data engineering roles open. Take a look and apply in minutes.</p>",
    "job_link_blocks": [],
}
SEEDED = [
    ("Asha Rao", "asha.rao.e78@example.com", "+919876500001"),
    ("Bala Krishnan", "bala.k.e78@example.com", "+919876500002"),
    ("Chitra Iyer", "chitra.iyer.e78@example.com", "+919876500003"),
]


@dataclasses.dataclass
class Env:
    org: DOrg
    tenant_id: uuid.UUID
    campaign_id: uuid.UUID
    audience_id: uuid.UUID
    candidate_ids: list[uuid.UUID]

    @property
    def m(self) -> Any:
        return self.org.admin


async def enable(tenant_id: uuid.UUID, on: bool = True) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_campaign_copy": on})


async def setup(org: DOrg, *, feature: bool = True) -> Env:
    m = org.admin
    await setup_marketing(m)
    ids = [await candidate(m, name, email=email, phone=phone) for name, email, phone in SEEDED]
    aud = await audience(m, ids)
    camp = await email_campaign(m, aud["id"])
    await enable(org.tenant_id, feature)
    return Env(org, org.tenant_id, uuid.UUID(camp["id"]), uuid.UUID(aud["id"]), ids)


async def user_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> RequestContext:
    from app.services import entity_refs

    async with tenant_session(tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, tenant_id, user_id)
    assert isinstance(principal, Principal)
    return RequestContext(tenant_id=tenant_id, principal=principal)


def segments(n: int = 2) -> list[dict[str, Any]]:
    cities = ["Pune", "Chennai", "Hyderabad", "Kochi"]
    return [
        {"key": f"seg_{i}", "label": f"Data engineers in {cities[i % 4]}", "source": {"kind": "attribute", "field": "city", "value": cities[i % 4]}}
        for i in range(n)
    ]


def body(env: Env, **over: Any) -> dict[str, Any]:
    return {
        "campaign_id": str(env.campaign_id), "channel": "email",
        "brief": {"purpose": "new_jobs", "purpose_note": "New data engineering roles", "tone": "friendly", "call_to_action": "Apply now"},
        "segments": segments(2), "variants_per_segment": 2, **over,
    }  # fmt: skip


def script(segs: list[dict[str, Any]], variants: int, variant: dict[str, Any] | None = None, *, per_variant: list[dict[str, Any]] | None = None) -> list[FakeTurn]:
    v = variant or GOOD_EMAIL
    result = {
        "segments": [
            {"key": s["key"], "variants": [dict((per_variant or [v] * variants)[i]) for i in range(variants)]}
            for s in segs
        ]
    }
    return [
        FakeTurn("tool_calls", calls=tuple((t, {}) for t in READ_TOOLS[:2])),
        FakeTurn("tool_calls", calls=tuple((t, {}) for t in READ_TOOLS[2:])),
        FakeTurn("emit_result", result=result),
    ]


async def request(env: Env, data: dict[str, Any], turns: list[FakeTurn] | None = None, *, ctx: RequestContext | None = None) -> tuple[Any, FakeLLM]:
    ctx = ctx or await user_ctx(env.tenant_id, env.m.user_id)
    parsed = CopyRequestIn.model_validate(data)
    with fake_llm(turns or script(data["segments"], data["variants_per_segment"])) as fake:
        async with tenant_session(env.tenant_id) as db:
            out = await campaign_copy_service.request_copy(ctx, db, parsed)
    return out, fake


async def detail(env: Env, request_id: uuid.UUID) -> Any:
    ctx = await user_ctx(env.tenant_id, env.m.user_id)
    async with tenant_session(env.tenant_id) as db:
        return await campaign_copy_service.get_request(ctx, db, request_id)


async def call(env: Env, fn: str, *args: Any, ctx: RequestContext | None = None) -> Any:
    ctx = ctx or await user_ctx(env.tenant_id, env.m.user_id)
    async with tenant_session(env.tenant_id) as db:
        return await getattr(campaign_copy_service, fn)(ctx, db, *args)


async def drafts(env: Env, request_id: uuid.UUID) -> list[CampaignCopyDraft]:
    async with tenant_session(env.tenant_id) as db:
        rows = list((await db.execute(select(CampaignCopyDraft).where(CampaignCopyDraft.request_id == request_id).order_by(CampaignCopyDraft.segment_key, CampaignCopyDraft.variant_no))).scalars())
        for r in rows:
            db.expunge(r)
    return rows


async def request_row(env: Env, request_id: uuid.UUID) -> CampaignCopyRequest:
    async with tenant_session(env.tenant_id) as db:
        row = await db.get(CampaignCopyRequest, request_id)
        assert row is not None
        db.expunge(row)
    return row


async def opened_long_ago(env: Env, draft_id: uuid.UUID, seconds: int = 60) -> None:
    from datetime import UTC, datetime, timedelta

    async with tenant_session(env.tenant_id) as db:
        d = await db.get(CampaignCopyDraft, draft_id)
        assert d is not None
        d.opened_at = datetime.now(UTC) - timedelta(seconds=seconds)
