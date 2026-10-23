# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tools on candidates (FR-277, NFR-46; coordinator decision C-8: Copilot owns
``candidates.search``, distinct from Epic 56's ``search.search_candidates``).

* ``candidates.search`` -- structured filters only (the model supplies them; no ``nl`` AI call). Request mapping:
  ``keywords`` + ``skills`` -> the boolean ``q`` (quoted terms, implicit AND); ``location`` -> ``filters.cities``;
  ``min_experience_years`` -> ``experience_min``; ``max_notice_days`` -> ``notice_max_days``;
  ``available_within_days`` -> ``available_by`` (today + n); ``tags`` -> ``filters.tags``; ``limit`` -> ``page_size``.
* ``candidates.get`` -- a subset of the 360 view, compensation masked by the domain unless ``compensation:view``;
  no identity fields (no email, phone, documents).
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, plural, scoped, url_for


def _quoted(term: str) -> str:
    clean = " ".join(term.replace('"', " ").split())
    return f'"{clean}"' if clean else ""


# ================================================================ candidates.search
class CandidateSearchToolIn(In):
    keywords: str | None = Field(default=None, max_length=200)
    skills: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=10
    )
    location: str | None = Field(default=None, max_length=80)
    min_experience_years: int | None = Field(default=None, ge=0, le=50)
    max_notice_days: int | None = Field(default=None, ge=0, le=365)
    available_within_days: int | None = Field(default=None, ge=0, le=365)
    tags: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=10, ge=1, le=25)


class CandidateRow(Out):
    id: uuid.UUID
    human_id: str | None
    full_name: str
    current_title: str | None
    city: str | None
    experience_years: float | None
    top_skills: list[str]
    owner_id: uuid.UUID | None
    url: str | None


class CandidateSearchToolOut(Out):
    total: int
    rows: list[CandidateRow]


async def _search(tctx: Any, args: CandidateSearchToolIn) -> ToolOutcome:
    from app.schemas.search import CandidateSearchFilters, CandidateSearchIn
    from app.services.search import candidate_search

    terms = ([args.keywords] if args.keywords else []) + list(args.skills)
    q = " ".join(filter(None, (_quoted(t) for t in terms))) or None
    filters = CandidateSearchFilters(
        cities=[args.location] if args.location else [],
        experience_min=Decimal(args.min_experience_years) if args.min_experience_years is not None else None,
        notice_max_days=args.max_notice_days,
        available_by=(datetime.now(UTC) + timedelta(days=args.available_within_days)).date()
        if args.available_within_days is not None
        else None,
        tags=list(args.tags),
    )
    body = CandidateSearchIn(
        q=q, mode="boolean", filters=filters, sort="relevance", page=1, page_size=args.limit
    )
    out = await candidate_search.search_candidates(scoped(tctx), tctx.db, body)
    rows = [
        CandidateRow(
            id=h.id, human_id=h.human_id, full_name=h.display_name, current_title=h.current_title, city=h.city,
            experience_years=h.experience_years, top_skills=[s.name for s in h.skills][:5], owner_id=h.owner_id,
            url=url_for("candidate", h.id),
        )
        for h in out.items[: args.limit]
    ]  # fmt: skip
    ids = tuple(r.id for r in rows)
    return ToolOutcome(
        CandidateSearchToolOut(total=out.total, rows=rows),
        refs=tuple(("candidate", i) for i in ids),
        candidate_ids=ids,
        summary=plural(out.total, "candidate"),
    )


# ================================================================ candidates.get
class CandidateGetIn(In):
    candidate_id: uuid.UUID


class SkillItem(Out):
    name: str
    proficiency: int | None
    verified: bool


class OpenApplication(Out):
    application_id: uuid.UUID
    demand_id: uuid.UUID | None
    demand_title: str | None
    stage_key: str | None


class ExpectedComp(Out):
    amount: str | None
    currency: str | None
    unit: str | None


class CandidateGetOut(Out):
    id: uuid.UUID
    human_id: str | None
    full_name: str
    current_title: str | None
    current_employer: str | None
    experience_years: float | None
    city: str | None
    notice_period_days: int | None
    availability_date: str | None
    skills: list[SkillItem]
    owner_id: uuid.UUID | None
    expected_comp: ExpectedComp | None
    compensation_hidden: bool
    open_applications: list[OpenApplication]
    url: str | None


async def _get(tctx: Any, args: CandidateGetIn) -> ToolOutcome:
    from app.services.candidates import records

    data = await records.get_candidate_360(scoped(tctx), tctx.db, args.candidate_id)
    months = data.get("total_experience_months")
    location = data.get("location") or {}
    masked_fields = set(data.get("masked_fields") or [])
    hidden = any("ctc" in f or "comp" in f for f in masked_fields)
    amount = data.get("expected_ctc_amount")
    skills = [
        SkillItem(
            name=str(((s.get("skill") or {}).get("name")) or "")[:80],
            proficiency=s.get("proficiency"),
            verified=s.get("verification") == "verified",
        )
        for s in (data.get("skills") or [])[:20]
    ]
    apps = [
        OpenApplication(
            application_id=a["id"], demand_id=a.get("demand_id"), demand_title=(a.get("demand_title") or None),
            stage_key=((a.get("stage") or {}).get("key")),
        )
        for a in (data.get("applications") or [])[:10]
        if a.get("id")
    ]  # fmt: skip
    out = CandidateGetOut(
        id=data["id"], human_id=data.get("human_id"), full_name=data.get("full_name") or "", current_title=data.get("current_title"),
        current_employer=data.get("current_employer"), experience_years=round(months / 12, 1) if months is not None else None,
        city=location.get("city"), notice_period_days=data.get("notice_period_days"),
        availability_date=str(data["availability_date"]) if data.get("availability_date") else None, skills=skills,
        owner_id=data.get("owner_id"),
        expected_comp=None if hidden or amount is None else ExpectedComp(
            amount=str(amount), currency=data.get("expected_ctc_currency"), unit=data.get("expected_ctc_unit")
        ),
        compensation_hidden=hidden, open_applications=apps, url=url_for("candidate", data["id"]),
    )  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("candidate", out.id)]
    refs += [("application", a.application_id) for a in apps]
    refs += [("demand", a.demand_id) for a in apps if a.demand_id]
    return ToolOutcome(out, refs=tuple(refs), candidate_ids=(out.id,), summary="candidate read")


register_tool(
    ToolSpec(
        key="candidates.search",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Searched candidates",
        description=(
            "Find candidates you can see with structured filters: keywords, skills (names), city, minimum years of "
            "experience, maximum notice period, availability window and tag ids. Returns the total and the top rows "
            "with links. It never shows candidates outside your access, never contacts anyone and does not rank for "
            "a demand (use matching__rank for that)."
        ),
        input_model=CandidateSearchToolIn,
        output_model=CandidateSearchToolOut,
        permissions=("candidate:read",),
        risk="read",
        scope=ScopeRule(resource="candidate", cross_subject=True),
        handler=_search,
        untrusted_output=frozenset({"rows.current_title"}),
    )
)

register_tool(
    ToolSpec(
        key="candidates.get",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Read candidate",
        description=(
            "Read one candidate you can see: title, employer, experience, city, notice, availability, skills, owner, "
            "expected compensation (null when hidden by your access) and open applications. It returns no contact "
            "details or identity documents and changes nothing. An unknown or inaccessible id returns not found."
        ),
        input_model=CandidateGetIn,
        output_model=CandidateGetOut,
        permissions=("candidate:read",),
        risk="read",
        scope=ScopeRule(
            resource="candidate", target_arg="candidate_id", cross_subject=True, candidate_arg="candidate_id"
        ),
        handler=_get,
        untrusted_output=frozenset({"current_title", "current_employer"}),
    )
)
