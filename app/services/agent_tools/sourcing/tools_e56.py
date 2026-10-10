# ruff: noqa: E501
"""Epic 56 Sourcing Agent tools of the ``sourcing`` domain (``owner_epic="56"``, ``version=1``).

| Key | Risk | Notes |
|---|---|---|
| ``candidates.engagement`` | read | last activity / contact, open applications, notice, posting link (drafting facts) |
| ``shortlists.get_for_demand`` | read | the current agent shortlist, the Demand facts and the run settings |
| ``shortlists.save_suggested`` | low (auto) | ids + source tags only; the server builds the set, excludes, ranks and saves |
| ``outreach.create_draft`` | low (auto) | validates + stores drafts (internal) and creates one ``outreach.send`` Proposal per draft |
| ``outreach.send`` | high | egress, free-text email; only ever a Proposal; executes as the approving person |

Deviation (recorded): ``outreach.create_draft`` takes up to 25 drafts in one call and creates the ``outreach.send``
Proposals itself (``agent_runtime_service.create_draft_for``), so drafting the top 10 fits the platform write cap
(``max_writes=10``) and the agent does not need a second call per Candidate.
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.core.agentic import (
    IdempotencyRule,
    ProposalPreview,
    Recipient,
    ScopeRule,
    SendIntent,
    ToolOutcome,
    ToolSpec,
    register_tool,
)
from app.core.errors import DomainError, NotFound, ValidationFailed
from app.services import agent_runtime_service as runtime
from app.services.agent_tools.sourcing.common_e56 import OWNER_EPIC, VERSION, In, Out, scoped

SourceKind = Literal["match", "pool", "silver_medalist", "search", "rediscovery"]
Channel = Literal["email", "sms", "whatsapp"]


def _svc() -> Any:
    from app.services import sourcing_agent_service

    return sourcing_agent_service


async def _demand_label(tctx: Any, demand_id: uuid.UUID) -> str:
    from app.services import entity_refs

    return (await entity_refs.entity_label(tctx.db, "demand", demand_id)) or "the Demand"


async def _candidate_label(tctx: Any, candidate_id: uuid.UUID) -> str:
    from app.services import entity_refs

    return (await entity_refs.entity_label(tctx.db, "candidate", candidate_id)) or "the candidate"


# ================================================================ candidates.engagement
class EngagementIn(In):
    candidate_id: uuid.UUID
    demand_id: uuid.UUID


class EngagementOut(Out):
    last_activity_at: datetime | None
    last_contacted_at: datetime | None
    open_applications: int
    notice_period_days: int | None
    availability_date: date | None
    posting_url: str | None


async def _engagement(tctx: Any, args: EngagementIn) -> ToolOutcome:
    subject = tctx.subject
    if subject is not None and subject[0] == "demand" and subject[1] != args.demand_id:
        raise NotFound("Demand not found")
    facts = await _svc().engagement(scoped(tctx), tctx.db, args.candidate_id, args.demand_id)
    return ToolOutcome(
        EngagementOut(**facts), candidate_ids=(args.candidate_id,), target=("candidate", args.candidate_id),
        summary="engagement facts read",
    )  # fmt: skip


# ================================================================ shortlists.get_for_demand
class GetIn(In):
    demand_id: uuid.UUID


class SkillRef(Out):
    skill_id: uuid.UUID | None
    label: str


class DemandFacts(Out):
    title: str
    status: str
    location: str | None
    work_mode: str | None
    experience_min: float | None
    experience_max: float | None
    must_have: list[SkillRef]
    nice_to_have: list[SkillRef]


class RunSettings(Out):
    top_n: int
    draft_top: int
    preferred_channel: str
    max_searches: int
    propose_pipeline_adds: bool
    propose_sequence_id: uuid.UUID | None


class EntryBrief(Out):
    entry_id: uuid.UUID
    candidate_id: uuid.UUID
    rank: int
    overall: float | None
    sources: list[str]
    explanation: str | None
    status: str
    best_channel: str | None
    flags: list[str]
    draft_status: str


class GetOut(Out):
    shortlist_id: uuid.UUID | None
    version: int | None
    demand: DemandFacts
    settings: RunSettings
    entries: list[EntryBrief]


def _brief(e: Any) -> EntryBrief:
    return EntryBrief(
        entry_id=e.id, candidate_id=e.candidate_id, rank=e.rank, overall=float(e.overall) if e.overall is not None else None,
        sources=sorted({str(s.get("kind")) for s in e.sources or [] if isinstance(s, dict)}),
        explanation=e.explanation, status=e.status, best_channel=e.best_channel, flags=list(e.flags or []),
        draft_status=e.draft_status,
    )  # fmt: skip


async def _get(tctx: Any, args: GetIn) -> ToolOutcome:
    data = await _svc().get_for_demand(scoped(tctx), tctx.db, args.demand_id)
    info, s, row, entries = data["info"], data["settings"], data["shortlist"], data["entries"]

    def _f(v: Any) -> float | None:
        return float(v) if v is not None else None

    out = GetOut(
        shortlist_id=row.id if row is not None else None, version=row.version if row is not None else None,
        demand=DemandFacts(
            title=info["title"], status=info["status"], location=info["location"], work_mode=info["work_mode"],
            experience_min=_f(info["experience_min"]), experience_max=_f(info["experience_max"]),
            must_have=[SkillRef(**x) for x in info["must_have"]][:20], nice_to_have=[SkillRef(**x) for x in info["nice_to_have"]][:20],
        ),
        settings=RunSettings(
            top_n=s.top_n, draft_top=s.draft_top, preferred_channel=s.preferred_channel, max_searches=s.max_searches,
            propose_pipeline_adds=s.propose_pipeline_adds, propose_sequence_id=s.propose_sequence_id,
        ),
        entries=[_brief(e) for e in entries],
    )  # fmt: skip
    refs: list[tuple[str, uuid.UUID]] = [("agent_shortlist_entry", e.id) for e in entries]
    if row is not None:
        refs.insert(0, ("agent_shortlist", row.id))
    return ToolOutcome(
        out, refs=tuple(refs), candidate_ids=tuple(e.candidate_id for e in entries), target=("demand", args.demand_id),
        summary=f"shortlist v{row.version if row else 0} with {len(entries)} entries",
    )  # fmt: skip


# ================================================================ shortlists.save_suggested
class SourceTag(In):
    kind: SourceKind
    ref: str | None = Field(default=None, max_length=80)


class Suggested(In):
    candidate_id: uuid.UUID
    sources: list[SourceTag] = Field(default_factory=list, max_length=5)
    note: str | None = Field(default=None, max_length=300)


class SaveIn(In):
    demand_id: uuid.UUID
    candidates: list[Suggested] = Field(default_factory=list, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)


class SaveOut(Out):
    shortlist_id: uuid.UUID
    version: int
    saved: int
    top_n: int
    draft_top: int
    excluded_counts: dict[str, int]
    dropped_not_found: int
    entries: list[EntryBrief]


async def _save(tctx: Any, args: SaveIn) -> ToolOutcome:
    from app.services.agent_runtime.sanitize import outbound

    summary = outbound(args.summary, max_len=2000).text if args.summary else None
    result = await _svc().save_suggested(
        scoped(tctx), tctx.request, tctx.db, args.demand_id, run_id=tctx.run_id,
        candidates=[
            {"candidate_id": c.candidate_id, "sources": [t.model_dump() for t in c.sources],
             "note": outbound(c.note, max_len=300).text if c.note else None}
            for c in args.candidates
        ],
        summary=summary,
    )  # fmt: skip
    shortlist, entries = result["shortlist"], result["entries"]
    return ToolOutcome(
        SaveOut(
            shortlist_id=shortlist.id,
            version=shortlist.version,
            saved=len(entries),
            top_n=result["top_n"],
            draft_top=result["draft_top"],
            excluded_counts=dict(result["excluded_counts"]),
            dropped_not_found=len(result["dropped_not_found"]),
            entries=[_brief(e) for e in entries],
        ),
        refs=(("agent_shortlist", shortlist.id), *(("agent_shortlist_entry", e.id) for e in entries)),
        candidate_ids=tuple(e.candidate_id for e in entries),
        target=("demand", args.demand_id),
        summary=f"shortlist v{shortlist.version} saved ({len(entries)})",
    )


async def _save_preview(tctx: Any, args: SaveIn) -> ProposalPreview:
    label = await _demand_label(tctx, args.demand_id)
    return ProposalPreview(
        title=f"Save agent shortlist ({len(args.candidates)}) for {label}"[:200],
        summary=(args.summary or "The agent proposes an updated shortlist in match-score order.")[:4000],
        fields=(("demand_id", str(args.demand_id)), ("Candidates proposed", str(len(args.candidates)))),
    )


# ================================================================ outreach.create_draft
class DraftItem(In):
    entry_id: uuid.UUID | None = Field(default=None, description="Shortlist entry id (or give candidate_id)")
    candidate_id: uuid.UUID | None = Field(default=None, description="Candidate of the current shortlist")
    channel: Channel = "email"
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class DraftsIn(In):
    demand_id: uuid.UUID
    drafts: list[DraftItem] = Field(min_length=1, max_length=25)


class DraftResult(Out):
    entry_id: uuid.UUID | None
    candidate_id: uuid.UUID | None = None
    status: Literal["proposed", "refused"]
    reason: str | None = None
    flags: list[str] = Field(default_factory=list)
    proposal: str | None = None


class DraftsOut(Out):
    items: list[DraftResult]
    proposed: int


async def _drafts(tctx: Any, args: DraftsIn) -> ToolOutcome:
    svc = _svc()
    read_ctx = scoped(tctx)
    items: list[DraftResult] = []
    refs: list[tuple[str, uuid.UUID]] = []
    seen: set[uuid.UUID] = set()
    for d in args.drafts:
        key = d.entry_id or d.candidate_id
        if key is None or key in seen:
            items.append(
                DraftResult(
                    entry_id=d.entry_id,
                    candidate_id=d.candidate_id,
                    status="refused",
                    reason="duplicate" if key else "entry_required",
                )
            )
            continue
        seen.add(key)
        try:
            async with tctx.db.begin_nested():
                entry = await svc.current_entry_for(
                    read_ctx, tctx.db, args.demand_id, entry_id=d.entry_id, candidate_id=d.candidate_id
                )
                text, subject, flags = await svc.prepare_draft(
                    read_ctx, tctx.db, entry, channel=d.channel, subject=d.subject, body=d.body
                )
                proposal = await runtime.create_draft_for(
                    tctx, "outreach.send",
                    {"entry_id": str(entry.id), "candidate_id": str(entry.candidate_id), "channel": d.channel,
                     "subject": subject, "body": text},
                )  # fmt: skip
                await svc.attach_proposal(tctx.db, entry, proposal.id)
        except NotFound:
            items.append(
                DraftResult(
                    entry_id=d.entry_id, candidate_id=d.candidate_id, status="refused", reason="not_found"
                )
            )
            continue
        except DomainError as exc:
            items.append(
                DraftResult(
                    entry_id=d.entry_id, candidate_id=d.candidate_id, status="refused", reason=exc.code[:50]
                )
            )
            continue
        refs.append(("agent_proposal", proposal.id))
        items.append(
            DraftResult(
                entry_id=entry.id,
                candidate_id=entry.candidate_id,
                status="proposed",
                flags=list(flags),
                proposal=proposal.human_id,
            )
        )
    proposed = sum(1 for i in items if i.status == "proposed")
    if proposed == 0:
        first = items[0].reason or "invalid"
        if first == "not_found" and all(i.reason == "not_found" for i in items):
            raise NotFound("Shortlist entry not found")
        raise ValidationFailed(
            "No draft could be created",
            code=first,
            details={"items": [i.model_dump(mode="json") for i in items]},
        )
    return ToolOutcome(
        DraftsOut(items=items, proposed=proposed), refs=tuple(refs), target=("demand", args.demand_id),
        summary=f"{proposed} outreach proposals created",
    )  # fmt: skip


async def _drafts_preview(tctx: Any, args: DraftsIn) -> ProposalPreview:
    label = await _demand_label(tctx, args.demand_id)
    return ProposalPreview(
        title=f"Draft outreach ({len(args.drafts)}) for {label}"[:200],
        summary="\n\n".join(d.body for d in args.drafts)[:4000],
        fields=(("demand_id", str(args.demand_id)), ("Drafts", str(len(args.drafts)))),
    )


# ================================================================ outreach.send
class SendIn(In):
    entry_id: uuid.UUID
    candidate_id: uuid.UUID
    channel: Channel = "email"
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class SendOut(Out):
    message_id: uuid.UUID | None
    status: str


async def _entry_version(probe: Any) -> str | None:
    return await _svc().entry_version(probe.db, probe.args.entry_id)


async def _send_intent(tctx: Any, args: SendIn) -> SendIntent:
    from app.services import entity_refs

    if not await entity_refs.can_read(scoped(tctx), tctx.db, "candidate", args.candidate_id):
        raise NotFound("Candidate not found")
    return SendIntent(
        recipients=(Recipient(kind="candidate", id=args.candidate_id, channel=args.channel),),
        templated=False, template_ref=None, text_for_checks=args.body,
    )  # fmt: skip


async def _send(tctx: Any, args: SendIn) -> ToolOutcome:
    if tctx.execution == "agent":  # defence in depth: risk 'high' never reaches auto
        raise ValidationFailed("Outreach needs a person's approval", code="proposal_required")
    approver = tctx.request.principal
    message_id, status = await _svc().send_outreach(
        tctx.request, tctx.db, args.entry_id, args.candidate_id, channel=args.channel, subject=args.subject,
        body=args.body, owner_user_id=tctx.owner_user_id, approver_id=approver.id,
        refs={"candidate_id": args.candidate_id},
    )  # fmt: skip
    return ToolOutcome(
        SendOut(message_id=message_id, status=status), candidate_ids=(args.candidate_id,),
        target=("agent_shortlist_entry", args.entry_id), summary="outreach queued",
    )  # fmt: skip


async def _send_preview(tctx: Any, args: SendIn) -> ProposalPreview:
    from sqlalchemy import select

    from app.models.sourcing_agent import AgentShortlistEntry

    demand_id = (
        await tctx.db.execute(
            select(AgentShortlistEntry.demand_id).where(AgentShortlistEntry.id == args.entry_id)
        )
    ).scalar_one_or_none()
    name = await _candidate_label(tctx, args.candidate_id)
    subject = args.subject or "A role you may be interested in"
    return ProposalPreview(
        title=f"Send {args.channel} to {name}: {subject}"[:200],
        summary=args.body[:4000],
        fields=(
            ("Recipient", name),
            ("Channel", args.channel),
            ("Subject", subject),
            ("demand_id", str(demand_id) if demand_id else ""),
            ("entry_id", str(args.entry_id)),
        ),
        renderer="outreach.send",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="candidates.engagement", version=VERSION, owner_epic=OWNER_EPIC, title="Candidate engagement facts",
        description="Read drafting facts for one candidate and the Demand: last activity, last contact, open applications, notice period, availability and the public posting link. Read only.",
        input_model=EngagementIn, output_model=EngagementOut, permissions=("candidate:read",), risk="read",
        scope=ScopeRule(resource="candidate", target_arg="candidate_id", candidate_arg="candidate_id"), handler=_engagement,
    ),
    ToolSpec(
        key="shortlists.get_for_demand", version=VERSION, owner_epic=OWNER_EPIC, title="Agent shortlist",
        description="Read the Demand's facts (title, location, must-have skill ids), the sourcing settings for this run (shortlist size, drafts, searches, whether pipeline adds / sequence enrolment may be proposed) and the current agent shortlist. Call it first. Read only.",
        input_model=GetIn, output_model=GetOut, permissions=("match:read", "demand:read"), risk="read",
        scope=ScopeRule(resource="demand", target_arg="demand_id"), handler=_get,
        untrusted_output=frozenset({"entries.explanation"}),
    ),
    ToolSpec(
        key="shortlists.save_suggested", version=VERSION, owner_epic=OWNER_EPIC, title="Save agent shortlist",
        description="Save the agent shortlist for the Demand. Pass candidate ids with source tags (and an optional short note) only: the server adds the match list, pinned Pools and silver medalists, applies exclusions (pipeline, do-not-contact, placed, archived, consent) and ranks by the stored match score. You cannot score or reorder. Call it once.",
        input_model=SaveIn, output_model=SaveOut, permissions=("match:read", "note:write"), risk="low",
        scope=ScopeRule(resource="demand", target_arg="demand_id"), handler=_save, preview=_save_preview, default_mode="auto",
        idempotency=IdempotencyRule(fields=("demand_id",), per_run=True),
        untrusted_input=frozenset({"candidates.note", "summary"}), max_calls_per_run=3,
    ),
    ToolSpec(
        key="outreach.create_draft", version=VERSION, owner_epic=OWNER_EPIC, title="Draft outreach",
        description="Draft personalised outreach emails (up to 25) for entries of the current agent shortlist. Each draft must stay within the published pay range, mention no protected attribute and have at most 150 words; opt-out text is added. Each accepted draft becomes a send proposal a person approves; nothing is sent by this action. SMS / WhatsApp need an approved template.",
        input_model=DraftsIn, output_model=DraftsOut, permissions=("message:draft",), risk="low",
        scope=ScopeRule(resource="demand", target_arg="demand_id"), handler=_drafts, preview=_drafts_preview,
        default_mode="auto", untrusted_input=frozenset({"drafts.body", "drafts.subject"}), max_calls_per_run=3,
    ),
    ToolSpec(
        key="outreach.send", version=VERSION, owner_epic=OWNER_EPIC, title="Send outreach",
        description="Send an approved outreach email to a shortlisted candidate. Always reviewed and sent by a person; consent, opt-outs and the weekly frequency cap are checked again at sending.",
        input_model=SendIn, output_model=SendOut, permissions=("message:draft", "email:send"), risk="high",
        scope=ScopeRule(resource="agent_shortlist_entry", target_arg="entry_id", candidate_arg="candidate_id", version=_entry_version),
        handler=_send, preview=_send_preview, egress=True, send_intent=_send_intent,
        untrusted_input=frozenset({"body", "subject"}),
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
