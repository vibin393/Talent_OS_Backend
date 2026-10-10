# ruff: noqa: E501
"""Epic 78 read tools of the ``campaign_copy`` agent (AD-91, FR-401). Risk ``read``, permission ``campaign:read``.

The run's copy request is resolved from the run id (``campaign_copy_requests.agent_run_id``); the tools take no ids.
**No recipient data is reachable**: no tool reads candidates, audience members, messages or engagement rows -- the
brief, AGGREGATE segment specs with reachable counts, linked postings' public fields, the chosen template and the
Tenant's link / footer rules only. Posting text, segment labels and the brief note are untrusted (wrapped).

Reads models directly (``p5-below``: P4 tool packages never import the P5 ``campaign_copy`` package).
"""

import uuid
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import NotFound

OWNER_EPIC = "78"
VERSION = 1
READ = ("campaign:read",)
NONE = ScopeRule(resource="none")
# P3 campaign rendering resolves these groups (plus ``{{job_link.N}}``).
MERGE_GROUPS = ("candidate", "tenant", "sender")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class NoArgs(_In):
    pass


class BriefOut(_Out):
    channel: str
    target: str
    variants_per_segment: int
    purpose: str
    purpose_note: str
    tone: str
    call_to_action: str
    language: str
    segments: list[dict[str, Any]]


class PostingsOut(_Out):
    postings: list[dict[str, Any]]


class TemplatesOut(_Out):
    channel: str
    template: dict[str, Any] | None
    merge_fields: list[str]
    rules: list[str]


class StyleOut(_Out):
    company_name: str
    careers_url: str
    allowed_link_hosts: list[str]
    footer_contact_set: bool
    footer_rule: str


async def _request(tctx: Any) -> Any:
    from app.models.campaign_copy import CampaignCopyRequest

    if tctx.run_id is None:
        raise NotFound("No copy request for this run")
    req = (
        await tctx.db.execute(select(CampaignCopyRequest).where(CampaignCopyRequest.agent_run_id == tctx.run_id))
    ).scalar_one_or_none()
    if req is None:
        raise NotFound("No copy request for this run")
    return req


async def _reachable(tctx: Any, channel: str, source: dict[str, Any]) -> int | None:
    """Aggregate count only (the ids never leave this function)."""
    from app.models.campaigns import AudienceSource
    from app.services import agent_runtime_service as runtime
    from app.services.campaigns import audiences, governance
    from app.services.campaigns.common import now

    if source.get("kind") != "audience_source":
        return None
    row = await tctx.db.get(AudienceSource, uuid.UUID(str(source.get("id"))))
    if row is None:
        return 0
    try:
        ctx = runtime.scope_context(tctx.request, tctx.owner_user_id)
        ids = await audiences._source_ids(ctx, tctx.db, row)
        counts, _ = await governance.bulk_preview(tctx.db, ids, (channel,), now())
    except Exception:
        return None
    return int(counts.get(channel, 0))


async def _brief(tctx: Any, args: NoArgs) -> ToolOutcome:  # noqa: ARG001
    req = await _request(tctx)
    brief = dict(req.brief or {})
    segments = []
    for s in req.segments or []:
        source = dict(s.get("source") or {})
        described = (
            {"kind": "attribute", "field": source.get("field"), "value": source.get("value")}
            if source.get("kind") == "attribute"
            else {"kind": "audience_source"}
        )
        segments.append({"key": s["key"], "label": s["label"], "based_on": described, "reachable": await _reachable(tctx, req.channel, source)})
    out = BriefOut(
        channel=req.channel, target="campaign" if req.campaign_id else "sequence_step", variants_per_segment=req.variants_per_segment,
        purpose=str(brief.get("purpose") or "other"), purpose_note=str(brief.get("purpose_note") or ""), tone=str(brief.get("tone") or "friendly"),
        call_to_action=str(brief.get("call_to_action") or ""), language=str(brief.get("language") or "en"), segments=segments,
    )  # fmt: skip
    return ToolOutcome(out, summary=f"brief with {len(segments)} segments")


async def _postings(tctx: Any, args: NoArgs) -> ToolOutcome:  # noqa: ARG001
    from app.models.demands import Demand, DemandPosting, JdVersion
    from app.services.campaigns import tokens

    req = await _request(tctx)
    brief = dict(req.brief or {})
    posting_ids = [uuid.UUID(str(x)) for x in brief.get("posting_ids") or []]
    demand_ids = [uuid.UUID(str(x)) for x in brief.get("demand_ids") or []]
    rows: list[Any] = []
    if posting_ids:
        rows += list((await tctx.db.execute(select(DemandPosting).where(DemandPosting.id.in_(posting_ids)))).scalars())
    if demand_ids:
        rows += list((await tctx.db.execute(select(DemandPosting).where(DemandPosting.demand_id.in_(demand_ids), DemandPosting.is_live.is_(True)))).scalars())
    out: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for p in rows:
        if p.id in seen:
            continue
        seen.add(p.id)
        d = await tctx.db.get(Demand, p.demand_id)
        if d is None:
            continue
        jd = await tctx.db.get(JdVersion, p.jd_version_id) if p.jd_version_id else None
        pay = (
            {"min": str(d.pay_rate_min) if d.pay_rate_min is not None else None, "max": str(d.pay_rate_max) if d.pay_rate_max is not None else None,
             "currency": d.currency, "unit": d.rate_unit}
            if p.show_rate else None
        )  # fmt: skip
        out.append(
            {
                "posting_id": str(p.id), "title": d.title, "location": d.location if p.show_location else None,
                "work_mode": d.work_mode, "published_pay_range": pay, "description": (jd.content if jd is not None else "")[:3000],
                "careers_url": f"{tokens.base_url()}/careers/jobs/{p.id}",
            }
        )  # fmt: skip
    return ToolOutcome(PostingsOut(postings=out), summary=f"{len(out)} postings")


async def _templates(tctx: Any, args: NoArgs) -> ToolOutcome:  # noqa: ARG001
    from app.models.comms import ChannelTemplate
    from app.services.comms.merge_fields import MERGE_FIELDS

    req = await _request(tctx)
    template = None
    rules: list[str] = []
    if req.channel_template_id is not None:
        t = await tctx.db.get(ChannelTemplate, req.channel_template_id)
        if t is not None:
            variables = [str(v) for v in t.variables or []]
            template = {"name": t.name, "body": t.body, "variables": variables}
    if req.channel == "sms":
        rules = ["Fill every template variable in template_values.", "Each value is at most 30 characters.", "No URLs unless the variable is a link variable."]
    elif req.channel == "whatsapp":
        rules = ["Fill every template parameter in template_values.", "Each value is at most 1,024 characters.", "No newlines, tabs or 4+ spaces."]
    else:
        rules = [
            "Write subject (<= 78 characters recommended), preheader and body_html.",
            "Use only the merge fields listed; use {{job_link.N}} for job link N.",
            "Never add an unsubscribe link or hide the footer: it is added automatically.",
            "No scripts, forms, iframes or external images.",
        ]
    fields = sorted(p for p in MERGE_FIELDS if p.split(".", 1)[0] in MERGE_GROUPS)
    return ToolOutcome(TemplatesOut(channel=req.channel, template=template, merge_fields=fields, rules=rules), summary="templates and rules")


async def _style(tctx: Any, args: NoArgs) -> ToolOutcome:  # noqa: ARG001
    from app.models.campaigns import MarketingSettings
    from app.services import entity_refs
    from app.services.agent_runtime.sanitize import allowed_hosts
    from app.services.campaigns import tokens

    tenant = await entity_refs.tenant(tctx.db, tctx.request.tenant_id)
    row = (await tctx.db.execute(select(MarketingSettings))).scalar_one_or_none()
    hosts = sorted(allowed_hosts([urlsplit(tokens.base_url()).hostname or ""]))
    out = StyleOut(
        company_name=str(getattr(tenant, "name", "") or "")[:200], careers_url=f"{tokens.base_url()}/careers",
        allowed_link_hosts=hosts, footer_contact_set=bool(row is not None and (row.footer_contact or "").strip()),
        footer_rule="The postal / contact line and the unsubscribe link are appended at send; do not write them.",
    )  # fmt: skip
    return ToolOutcome(out, summary="tenant style")


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="campaign_copy.get_brief", version=VERSION, owner_epic=OWNER_EPIC, title="Copy brief",
        description="The copy request: channel, purpose, tone, call to action, variants per segment and the segments (label, what they are based on, aggregate reachable count). Never individual people. Read only.",
        input_model=NoArgs, output_model=BriefOut, permissions=READ, risk="read", scope=NONE, handler=_brief,
        untrusted_output=frozenset({"purpose_note", "call_to_action", "segments.label", "segments.based_on.value"}),
    ),
    ToolSpec(
        key="campaign_copy.get_postings", version=VERSION, owner_epic=OWNER_EPIC, title="Linked postings",
        description="Public fields of the job postings linked to the brief: title, location (if shown), work mode, published pay range (if shown), description, careers URL. Read only.",
        input_model=NoArgs, output_model=PostingsOut, permissions=READ, risk="read", scope=NONE, handler=_postings,
        untrusted_output=frozenset({"postings.title", "postings.description", "postings.location"}),
    ),
    ToolSpec(
        key="campaign_copy.get_templates", version=VERSION, owner_epic=OWNER_EPIC, title="Template and rules",
        description="The chosen SMS / WhatsApp template (body and variable names) and its limits, or the email merge fields and footer rules. Read only.",
        input_model=NoArgs, output_model=TemplatesOut, permissions=READ, risk="read", scope=NONE, handler=_templates,
        untrusted_output=frozenset({"template.body"}),
    ),
    ToolSpec(
        key="campaign_copy.get_tenant_style", version=VERSION, owner_epic=OWNER_EPIC, title="Company style",
        description="Company display name, careers URL, the link hosts copy may use and whether the marketing footer contact line is set. Read only.",
        input_model=NoArgs, output_model=StyleOut, permissions=READ, risk="read", scope=NONE, handler=_style,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
