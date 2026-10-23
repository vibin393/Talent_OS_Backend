# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tools on demands (FR-277, NFR-46): ``demands.list`` and ``demands.get`` through the demands
records facade (scope = the user's; rates masked unless ``compensation:view``). An out-of-scope REQ is simply absent
from a list and ``not found`` on a get -- byte-identical to a REQ that does not exist."""

import uuid
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import DomainError
from app.core.pagination import PageParams
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, as_uuid, plural, scoped, url_for

DemandStatus = Literal["draft", "pending_approval", "open", "on_hold", "filled", "cancelled", "closed"]
Priority = Literal["low", "medium", "high", "critical"]
SlaStatus = Literal["healthy", "at_risk", "breached", "met"]
SKILL_SCAN = 100


class DemandListIn(In):
    status: list[DemandStatus] = Field(default_factory=list, max_length=7)
    client_id: uuid.UUID | None = None
    owned_by: str | None = Field(default=None, max_length=36, description='"me" or a user id')
    priority: Priority | None = None
    sla_status: list[SlaStatus] = Field(default_factory=list, max_length=3)
    skill: str | None = Field(default=None, max_length=60)
    q: str | None = Field(default=None, max_length=120, description="Title or REQ id text")
    limit: int = Field(default=10, ge=1, le=25)


class DemandRow(Out):
    id: uuid.UUID
    human_id: str | None
    title: str
    client_name: str | None
    status: str
    priority: str | None
    openings: int | None
    sla_status: str | None
    owner_id: uuid.UUID | None
    created_at: str | None
    url: str | None


class DemandListOut(Out):
    total: int
    rows: list[DemandRow]


def _row(d: dict[str, Any]) -> DemandRow:
    return DemandRow(
        id=d["id"], human_id=d.get("req_id"), title=str(d.get("title") or "")[:200], client_name=d.get("client_name"),
        status=str(d.get("status")), priority=d.get("priority"), openings=d.get("positions"), sla_status=d.get("sla_status"),
        owner_id=d.get("owner_id"), created_at=str(d["created_at"]) if d.get("created_at") else None,
        url=url_for("demand", d["id"]),
    )  # fmt: skip


def _has_skill(d: dict[str, Any], skill: str) -> bool:
    needle = skill.casefold()
    return any(needle in str(s.get("label") or "").casefold() for s in d.get("skills") or [])


async def list_rows(
    tctx: Any, args: DemandListIn, *, sla_status: str | None = None
) -> tuple[int, list[dict[str, Any]]]:
    """Demands visible to the user (raw masked ``DemandOut`` dicts); also used by ``sla.at_risk``."""
    from app.schemas.demands import DemandFilters
    from app.services.demands import records

    owner_id = None
    if args.owned_by == "me":
        owner_id = tctx.owner_user_id
    elif args.owned_by:
        owner_id = as_uuid(args.owned_by)
    filters = DemandFilters(
        status=list(args.status) or None,
        client_id=args.client_id,
        priority=[args.priority] if args.priority else None,
        owner_id=owner_id,
        q=args.q or None,
        sla_status=sla_status,  # type: ignore[arg-type]
    )
    size = SKILL_SCAN if args.skill else args.limit
    page = await records.list_demands(scoped(tctx), tctx.db, filters, PageParams(page=1, page_size=size))
    items = list(page.items)
    if args.skill:
        items = [d for d in items if _has_skill(d, args.skill)]
        return len(items), items[: args.limit]
    return page.total, items


async def _list(tctx: Any, args: DemandListIn) -> ToolOutcome:
    if len(args.sla_status) > 1:
        total, items = 0, []
        for status in args.sla_status:
            t, rows = await list_rows(tctx, args, sla_status=status)
            total += t
            items += rows
        items = items[: args.limit]
    else:
        total, items = await list_rows(tctx, args, sla_status=args.sla_status[0] if args.sla_status else None)
    rows = [_row(d) for d in items]
    return ToolOutcome(
        DemandListOut(total=total, rows=rows),
        refs=tuple(("demand", r.id) for r in rows),
        summary=plural(total, "demand"),
    )


# ================================================================ demands.get
class DemandGetIn(In):
    demand_id: uuid.UUID


class SlaItem(Out):
    milestone: str
    status: str
    due_at: str | None


class DemandCounts(Out):
    applications: int | None
    submissions: int | None
    interviews: int | None
    offers: int | None
    filled: int | None


class DemandGetOut(Out):
    id: uuid.UUID
    human_id: str | None
    title: str
    client_id: uuid.UUID | None
    client_name: str | None
    status: str
    priority: str | None
    location: str | None
    work_mode: str | None
    experience_range: str | None
    openings: int | None
    skills: list[str]
    owner_id: uuid.UUID | None
    primary_recruiter_id: uuid.UUID | None
    bill_rate: str | None
    pay_rate_range: str | None
    rates_hidden: bool
    sla: list[SlaItem]
    counts: DemandCounts
    jd_excerpt: str | None
    url: str | None


async def _get(tctx: Any, args: DemandGetIn) -> ToolOutcome:
    from app.services.demands import jd, records

    ctx = scoped(tctx)
    d = await records.get_demand(ctx, tctx.db, args.demand_id)
    excerpt = None
    try:
        versions = await jd.list_jd_versions(ctx, tctx.db, args.demand_id)
        if versions:
            excerpt = versions[0].content[:2000]
    except DomainError:  # the JD is optional context
        excerpt = None
    masked = set(d.get("masked_fields") or [])
    lo, hi = d.get("min_experience_years"), d.get("max_experience_years")
    exp = f"{lo}-{hi} years" if lo is not None or hi is not None else None
    pay = None
    if d.get("pay_rate_min") is not None or d.get("pay_rate_max") is not None:
        pay = f"{d.get('pay_rate_min')}-{d.get('pay_rate_max')} {d.get('currency') or ''} {d.get('rate_unit') or ''}".strip()
    recruiter = d.get("primary_recruiter") or {}
    out = DemandGetOut(
        id=d["id"], human_id=d.get("req_id"), title=str(d.get("title") or ""), client_id=d.get("client_id"),
        client_name=d.get("client_name"), status=str(d.get("status")), priority=d.get("priority"),
        location=d.get("location"), work_mode=d.get("work_mode"), experience_range=exp, openings=d.get("positions"),
        skills=[str(s.get("label"))[:80] for s in (d.get("skills") or [])[:30]], owner_id=d.get("owner_id"),
        primary_recruiter_id=recruiter.get("id"),
        bill_rate=str(d["bill_rate"]) if d.get("bill_rate") is not None else None, pay_rate_range=pay,
        rates_hidden=bool(masked & {"bill_rate", "pay_rate_min", "pay_rate_max"}),
        sla=[SlaItem(milestone=str(c.get("milestone")), status=str(c.get("status")), due_at=str(c.get("due_at")) if c.get("due_at") else None) for c in d.get("sla") or []],
        counts=DemandCounts(
            applications=d.get("applications_count"), submissions=d.get("submissions_count"),
            interviews=d.get("interviews_scheduled_count"), offers=d.get("offers_released_count"), filled=d.get("positions_filled"),
        ),
        jd_excerpt=excerpt, url=url_for("demand", d["id"]),
    )  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("demand", out.id)]
    if out.client_id:
        refs.append(("client", out.client_id))
    return ToolOutcome(out, refs=tuple(refs), summary=f"read demand {out.human_id or ''}".strip())


register_tool(
    ToolSpec(
        key="demands.list",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Listed demands",
        description=(
            "List demands (requirements) you can see, filtered by status, client, owned_by ('me' or a user id), priority, "
            "SLA status, a skill name or title/REQ text. Returns the total and the top rows with links. Totals count "
            "only records you can see. It changes nothing and cannot open, close or approve a demand."
        ),
        input_model=DemandListIn,
        output_model=DemandListOut,
        permissions=("demand:read",),
        risk="read",
        scope=ScopeRule(resource="demand", cross_subject=True),
        handler=_list,
        untrusted_output=frozenset({"rows.title", "rows.client_name"}),
    )
)

register_tool(
    ToolSpec(
        key="demands.get",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Read demand",
        description=(
            "Read one demand you can see: client, status, priority, location, experience, openings, skills, owner, "
            "SLA clocks, pipeline counts and a JD excerpt. Rates are null when hidden by your access. It changes "
            "nothing. An unknown or inaccessible id returns not found."
        ),
        input_model=DemandGetIn,
        output_model=DemandGetOut,
        permissions=("demand:read",),
        risk="read",
        scope=ScopeRule(resource="demand", target_arg="demand_id", cross_subject=True),
        handler=_get,
        untrusted_output=frozenset({"jd_excerpt", "title"}),
    )
)
