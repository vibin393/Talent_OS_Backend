# ruff: noqa: E501
"""Facts the checks and the agent's read tools see (Epic 78): linked postings' PUBLIC fields, the chosen template, the
Tenant's allowed link hosts and aggregate segment counts. Never a recipient record."""

import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.campaign_copy import CampaignCopyRequest
from app.services.campaign_copy import checks

URL_VARIABLE_HINTS = ("url", "link")


async def postings(db: AsyncSession, brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Public fields of the linked postings (posting ids + demand ids) -- the careers projection only."""
    from app.models.demands import Demand, DemandPosting, JdVersion

    posting_ids = [uuid.UUID(str(x)) for x in brief.get("posting_ids") or []]
    demand_ids = [uuid.UUID(str(x)) for x in brief.get("demand_ids") or []]
    rows: list[DemandPosting] = []
    if posting_ids:
        rows += list((await db.execute(select(DemandPosting).where(DemandPosting.id.in_(posting_ids)))).scalars())
    if demand_ids:
        rows += list(
            (
                await db.execute(
                    select(DemandPosting).where(DemandPosting.demand_id.in_(demand_ids), DemandPosting.is_live.is_(True))
                )
            ).scalars()
        )
    out: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    from app.services.campaigns import tokens

    base = tokens.base_url()
    for p in rows:
        if p.id in seen:
            continue
        seen.add(p.id)
        demand = await db.get(Demand, p.demand_id)
        if demand is None:
            continue
        jd = await db.get(JdVersion, p.jd_version_id) if p.jd_version_id else None
        out.append(
            {
                "posting_id": str(p.id),
                "title": demand.title,
                "location": demand.location if p.show_location else None,
                "work_mode": demand.work_mode,
                "show_rate": bool(p.show_rate),
                "pay_min": str(demand.pay_rate_min) if p.show_rate and demand.pay_rate_min is not None else None,
                "pay_max": str(demand.pay_rate_max) if p.show_rate and demand.pay_rate_max is not None else None,
                "currency": demand.currency if p.show_rate else None,
                "rate_unit": demand.rate_unit if p.show_rate else None,
                "description": (jd.content if jd is not None else "")[:4000],
                "careers_url": f"{base}/careers/jobs/{p.id}",
            }
        )
    return out


async def template_facts(db: AsyncSession, template_id: uuid.UUID | None) -> dict[str, Any] | None:
    from app.models.comms import ChannelTemplate

    if template_id is None:
        return None
    t = await db.get(ChannelTemplate, template_id)
    if t is None:
        return None
    variables = [str(v) for v in (t.variables or [])]
    return {
        "id": str(t.id),
        "channel": t.channel,
        "name": t.name,
        "body": t.body,
        "variables": variables,
        "url_variables": [v for v in variables if any(h in v.lower() for h in URL_VARIABLE_HINTS)],
        "limits": {"value_max": checks.DLT_VALUE_MAX if t.channel == "sms" else checks.WA_VALUE_MAX},
    }


def allowed_link_hosts() -> frozenset[str]:
    from app.services.agent_runtime.sanitize import allowed_hosts
    from app.services.campaigns import tokens

    extra = [urlsplit(tokens.base_url()).hostname or ""]
    return allowed_hosts(extra)


def brief_text(brief: dict[str, Any]) -> str:
    return f"{brief.get('purpose_note') or ''} {brief.get('call_to_action') or ''}"


async def check_context(db: AsyncSession, req: CampaignCopyRequest) -> checks.CheckContext:
    facts = await postings(db, dict(req.brief or {}))
    tpl = await template_facts(db, req.channel_template_id)
    return checks.CheckContext(
        channel=req.channel,
        postings=tuple(
            checks.PostingFacts(
                posting_id=p["posting_id"], title=p["title"] or "", location=p["location"], work_mode=p["work_mode"],
                show_rate=p["show_rate"], pay_min=Decimal(p["pay_min"]) if p["pay_min"] else None,
                pay_max=Decimal(p["pay_max"]) if p["pay_max"] else None, description=p["description"] or "",
            )
            for p in facts
        ),
        brief_text=brief_text(dict(req.brief or {})),
        allowed_hosts=allowed_link_hosts(),
        template=checks.TemplateFacts(tpl["body"], tuple(tpl["variables"]), tuple(tpl["url_variables"])) if tpl else None,
    )  # fmt: skip


async def reachable(ctx: RequestContext, db: AsyncSession, channel: str, source: dict[str, Any]) -> int | None:
    """Aggregate reachable count of a segment (audience-source segments only; attribute segments -> None)."""
    from app.models.campaigns import AudienceSource
    from app.services.campaigns import audiences, governance
    from app.services.campaigns.common import now

    if source.get("kind") != "audience_source":
        return None
    row = await db.get(AudienceSource, uuid.UUID(str(source.get("id"))))
    if row is None:
        return 0
    try:
        ids = await audiences._source_ids(ctx, db, row)
        counts, _ = await governance.bulk_preview(db, ids, (channel,), now())
    except Exception:
        return None
    return int(counts.get(channel, 0))


__all__ = ["allowed_link_hosts", "brief_text", "check_context", "postings", "reachable", "template_facts"]
