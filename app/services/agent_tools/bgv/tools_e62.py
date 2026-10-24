# ruff: file-ignore[line-too-long]
"""BGV Agent tools (Epic 62 A4; ``owner_epic="62"``, ``version=1``).

| Key | Risk | Notes |
|---|---|---|
| ``bgv.get_case_summary`` / ``bgv.list_overdue_checks`` | read | structured only (no third-party free text) |
| ``bgv.request_provider_status`` | low (auto) | connector status poll (system-to-system, not egress) |
| ``bgv.send_provider_follow_up`` | low (auto); high with ``custom_note`` | templated email to the provider contact on record (C-5 ``provider_contact``); the address is resolved server-side |
| ``bgv.draft_provider_message`` | high | non-templated message to the provider -- Proposal only |
| ``bgv.request_candidate_documents`` | high | extra document requests -- Proposal only |
| ``bgv.post_case_summary`` | low (auto) | internal note on the candidate timeline ("BGV pending summary (AI)") |

Every check id must belong to the run's BGV case. ★ never: adjudicate, waive, record a result, send candidate
documents, document links or identity numbers -- no such tool or argument exists.
"""

import contextlib
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
from app.core.agentic.types import RiskClass
from app.core.errors import NotFound
from app.services import chasing_service

OWNER_EPIC = "62"
CASE = ScopeRule(resource="bgv_case", target_arg="case_id")
CHECK = ScopeRule(resource="bgv_check", target_arg="check_id", cross_subject=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _case_of_run(tctx: Any) -> uuid.UUID | None:
    subject = getattr(tctx, "subject", None)
    return subject[1] if subject is not None and subject[0] == "bgv_case" else None


async def _check(tctx: Any, check_id: uuid.UUID) -> Any:
    from app.models.bgv import BgvCheck

    check = await tctx.db.get(BgvCheck, check_id)
    case_id = _case_of_run(tctx)
    if check is None or (case_id is not None and check.case_id != case_id):
        raise NotFound("BGV check not found")
    return check


async def _today(tctx: Any) -> Any:
    return await chasing_service.chases.local_today(tctx.db)


# ================================================================ reads
class CaseIn(_In):
    case_id: uuid.UUID


class CheckView(_Out):
    check_id: uuid.UUID
    type: str
    label: str
    status: str
    overdue: bool
    due_on: str | None


class DocView(_Out):
    label: str
    status: str


class DiscrepancyView(_Out):
    severity: str
    status: str
    response_due: str | None


class SummaryOut(_Out):
    case_id: uuid.UUID
    case_ref: str
    status: str
    owner_user_id: uuid.UUID | None
    checks: list[CheckView]
    document_requests: list[DocView]
    discrepancies: list[DiscrepancyView]


async def _summary(tctx: Any, args: CaseIn) -> ToolOutcome:
    data = SummaryOut.model_validate(
        await chasing_service.bgv_follow_ups.case_summary(tctx.db, args.case_id, await _today(tctx))
    )
    refs: list[tuple[str, uuid.UUID]] = [("bgv_case", args.case_id)] + [
        ("bgv_check", c.check_id) for c in data.checks
    ]
    if data.owner_user_id:
        refs.append(("user", data.owner_user_id))
    return ToolOutcome(
        data, refs=tuple(refs), target=("bgv_case", args.case_id), summary=f"{len(data.checks)} checks"
    )


class OverdueView(_Out):
    check_id: uuid.UUID
    type: str
    subject_label: str
    provider_id: uuid.UUID | None
    provider_name: str | None
    provider_kind: str | None
    has_connector: bool
    due_on: str | None
    days_overdue: int
    follow_ups_sent: int
    next_due_on: str | None
    follow_up_due_today: bool
    limit_reached: bool


class OverdueOut(_Out):
    checks: list[OverdueView]


async def _overdue(tctx: Any, args: CaseIn) -> ToolOutcome:
    from app.models.bgv import BgvCheck

    today = await _today(tctx)
    rows = await chasing_service.bgv_follow_ups.overdue_checks(tctx.db, args.case_id, today)
    views = []
    refs: list[tuple[str, uuid.UUID]] = [("bgv_case", args.case_id)]
    for r in rows:
        check = await tctx.db.get(BgvCheck, r.check_id)
        provider_id = check.provider_id if check is not None else None
        views.append(
            OverdueView(
                check_id=r.check_id, type=r.type, subject_label=r.subject_label, provider_id=provider_id,
                provider_name=r.provider_name, provider_kind=r.provider_kind, has_connector=r.has_connector,
                due_on=r.due_on.isoformat() if r.due_on else None, days_overdue=r.days_overdue, follow_ups_sent=r.follow_ups_sent,
                next_due_on=r.next_due_on.isoformat() if r.next_due_on else None,
                follow_up_due_today=r.next_due_on is not None and r.next_due_on <= today, limit_reached=r.limit_reached,
            )
        )  # fmt: skip
        refs.append(("bgv_check", r.check_id))
        if provider_id:
            refs.append(("bgv_provider", provider_id))
    return ToolOutcome(
        OverdueOut(checks=views),
        refs=tuple(refs),
        target=("bgv_case", args.case_id),
        summary=f"{len(views)} overdue checks",
    )


# ================================================================ bgv.request_provider_status
class CheckIn(_In):
    check_id: uuid.UUID


class StatusOut(_Out):
    follow_up_id: uuid.UUID
    status: str
    provider_status: str | None


async def _status(tctx: Any, args: CheckIn) -> ToolOutcome:
    check = await _check(tctx, args.check_id)
    fu = await chasing_service.bgv_follow_ups.request_status(
        tctx.request, tctx.db, check.id, case_id=check.case_id, run_id=tctx.run_id, today=await _today(tctx)
    )
    return ToolOutcome(
        StatusOut(follow_up_id=fu.id, status=fu.status, provider_status=fu.provider_status),
        refs=(("bgv_check", check.id),),
        target=("bgv_check", check.id),
        summary=f"status {fu.provider_status or fu.status}",
    )


async def _status_preview(tctx: Any, args: CheckIn) -> ProposalPreview:
    check = await _check(tctx, args.check_id)
    return ProposalPreview(
        title=f"Request the provider status of {check.subject_label}"[:200],
        summary="Ask the BGV provider for the status of this overdue check through the connector.",
        fields=(("Check", check.subject_label),),
        renderer="bgv.request_provider_status",
    )


# ================================================================ bgv.send_provider_follow_up
class FollowUpIn(_In):
    check_id: uuid.UUID
    custom_note: str | None = Field(default=None, max_length=300)


class FollowUpOut(_Out):
    follow_up_id: uuid.UUID
    seq: int
    email_message_id: uuid.UUID | None


def _follow_up_risk(args: Any) -> RiskClass:
    return "high" if getattr(args, "custom_note", None) else "low"


async def _follow_up_intent(tctx: Any, args: Any) -> SendIntent:
    check = await _check(tctx, args.check_id)
    if check.provider_id is None:
        raise NotFound("BGV provider not found")
    text = getattr(args, "custom_note", None) or getattr(args, "body", None) or ""
    templated = isinstance(args, FollowUpIn) and not args.custom_note
    return SendIntent(
        recipients=(Recipient(kind="provider_contact", id=check.provider_id, channel="email"),),
        templated=templated, template_ref="agent_bgv_status_request" if templated else None, text_for_checks=text,
    )  # fmt: skip


async def _follow_up(tctx: Any, args: FollowUpIn) -> ToolOutcome:
    check = await _check(tctx, args.check_id)
    note = None
    if args.custom_note:
        from app.services.agent_runtime.sanitize import outbound

        note = outbound(args.custom_note, max_len=300).text
    fu = await chasing_service.bgv_follow_ups.send_follow_up(
        tctx.request,
        tctx.db,
        check.id,
        case_id=check.case_id,
        custom_note=note,
        run_id=tctx.run_id,
        today=await _today(tctx),
    )
    return ToolOutcome(
        FollowUpOut(follow_up_id=fu.id, seq=fu.seq, email_message_id=fu.email_message_id),
        refs=(("bgv_check", check.id),),
        target=("bgv_check", check.id),
        summary=f"follow-up {fu.seq} sent",
    )


async def _follow_up_preview(tctx: Any, args: FollowUpIn) -> ProposalPreview:
    check = await _check(tctx, args.check_id)
    fields: list[tuple[str, str]] = [("Check", check.subject_label)]
    if args.custom_note:
        fields.append(("Note", args.custom_note))
    return ProposalPreview(
        title=f"Send a status request to the BGV provider: {check.subject_label}"[:200],
        summary='Templated "Agent · BGV status request" to the provider contact on record (no documents or identity numbers).',
        fields=tuple(fields),
        renderer="bgv.send_provider_follow_up",
    )


# ================================================================ bgv.draft_provider_message (high)
class DraftIn(_In):
    check_id: uuid.UUID
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)


class DraftOut(_Out):
    follow_up_id: uuid.UUID
    email_message_id: uuid.UUID | None


async def _draft(tctx: Any, args: DraftIn) -> ToolOutcome:
    check = await _check(tctx, args.check_id)
    fu = await chasing_service.bgv_follow_ups.draft_message(
        tctx.request,
        tctx.db,
        check.id,
        args.subject,
        args.body,
        proposal_id=getattr(tctx.request, "proposal_id", None),
    )
    return ToolOutcome(
        DraftOut(follow_up_id=fu.id, email_message_id=fu.email_message_id),
        refs=(("bgv_check", check.id),),
        target=("bgv_check", check.id),
        summary="message to provider sent",
    )


async def _draft_preview(tctx: Any, args: DraftIn) -> ProposalPreview:
    check = await _check(tctx, args.check_id)
    return ProposalPreview(
        title=f"Email the BGV provider: {args.subject}"[:200],
        summary=args.body[:4000],
        fields=(("Check", check.subject_label), ("Subject", args.subject)),
        renderer="bgv.draft_provider_message",
    )


# ================================================================ bgv.request_candidate_documents (high)
class DocsIn(_In):
    case_id: uuid.UUID
    document_keys: list[str] = Field(min_length=1, max_length=10)
    note: str | None = Field(default=None, max_length=300)


class DocsOut(_Out):
    request_ids: list[uuid.UUID]


async def _docs(tctx: Any, args: DocsIn) -> ToolOutcome:
    ids = await chasing_service.bgv_follow_ups.request_candidate_documents(
        tctx.request, tctx.db, args.case_id, [k[:60] for k in args.document_keys], args.note
    )
    return ToolOutcome(
        DocsOut(request_ids=ids), target=("bgv_case", args.case_id), summary=f"{len(ids)} documents requested"
    )


async def _docs_preview(tctx: Any, args: DocsIn) -> ProposalPreview:  # ruff: ignore[unused-function-argument]  # NOSONAR
    from app.services.bgv.common import doc_label

    labels = ", ".join(doc_label(k)[0] for k in args.document_keys)
    return ProposalPreview(
        title=f"Request documents from the candidate: {labels}"[:200],
        summary=(args.note or "Ask the candidate for these BGV documents.")[:4000],
        fields=(("Documents", labels),),
        renderer="bgv.request_candidate_documents",
    )


# ================================================================ bgv.post_case_summary
class PostSummaryIn(_In):
    case_id: uuid.UUID
    summary: str = Field(min_length=1, max_length=1500)


class PostSummaryOut(_Out):
    activity_id: uuid.UUID


async def _post_summary(tctx: Any, args: PostSummaryIn) -> ToolOutcome:
    from app.models.bgv import BgvCase
    from app.services import activity_service
    from app.services.agent_runtime.sanitize import outbound

    case = await tctx.db.get(BgvCase, args.case_id)
    if case is None:
        raise NotFound("BGV case not found")
    body = f"BGV pending summary (AI) -- {case.human_id}\n\n{outbound(args.summary, max_len=1500).text}"
    activity_id = await activity_service.add_system_entry(
        tctx.request,
        tctx.db,
        "candidate",
        case.candidate_id,
        body[:2000],
        {"source": "bgv_agent", "case_id": str(case.id)},
    )
    owner = await chasing_service.bgv_follow_ups.case_owner(tctx.db, case)
    if owner is not None:
        from app.services import notifications_service as ns

        with contextlib.suppress(Exception):
            await ns.deliver(
                tctx.db, "agent_nudge", [owner],
                ns.Content(title=f"BGV Agent: pending summary for {case.human_id}"[:300], entity_type="candidate",
                           entity_id=case.candidate_id, link_path=f"/bgv/cases/{case.id}"),
                event_id=f"bgv_summary:{case.id}:{tctx.idempotency_key}",
            )  # fmt: skip
    return ToolOutcome(
        PostSummaryOut(activity_id=activity_id),
        refs=(("bgv_case", case.id),),
        candidate_ids=(case.candidate_id,),
        target=("bgv_case", case.id),
        summary="summary posted",
    )


async def _post_summary_preview(tctx: Any, args: PostSummaryIn) -> ProposalPreview:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return ProposalPreview(
        title="Post a BGV pending summary for the case owner",
        summary=args.summary[:4000],
        renderer="bgv.post_case_summary",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="bgv.get_case_summary", version=1, owner_epic=OWNER_EPIC, title="Read the BGV case",
        description="Read the BGV case: status, owner, checks (type, status, overdue), document requests and discrepancies. Structured only; read-only.",
        input_model=CaseIn, output_model=SummaryOut, permissions=("bgv:read",), risk="read", scope=CASE, handler=_summary,
    ),
    ToolSpec(
        key="bgv.list_overdue_checks", version=1, owner_epic=OWNER_EPIC, title="List overdue BGV checks",
        description="List the case's provider checks past their due date with the provider, whether it has a connector, follow-ups sent, the next follow-up date and whether one is due today. Read-only.",
        input_model=CaseIn, output_model=OverdueOut, permissions=("bgv:read",), risk="read", scope=CASE, handler=_overdue,
    ),
    ToolSpec(
        key="bgv.request_provider_status", version=1, owner_epic=OWNER_EPIC, title="Request provider status",
        description="For a provider WITH a connector: request the status of an overdue check through the connector. Refused when no follow-up is due (follow_up_not_due) or at the limit (follow_up_limit).",
        input_model=CheckIn, output_model=StatusOut, permissions=("bgv:follow_up",), risk="low", scope=CHECK,
        handler=_status, preview=_status_preview, default_mode="auto",
        idempotency=IdempotencyRule(fields=("check_id",), per_run=True),
    ),
    ToolSpec(
        key="bgv.send_provider_follow_up", version=1, owner_epic=OWNER_EPIC, title="Email the provider a status request",
        description="For a provider WITHOUT a connector: send the templated status request to the provider contact on record. No documents, links or identity numbers. A custom_note makes it a proposal. Refused for connector providers (not_manual_provider), when not due or at the limit.",
        input_model=FollowUpIn, output_model=FollowUpOut, permissions=("bgv:follow_up", "email:send"), risk="low",
        scope=CHECK, handler=_follow_up, preview=_follow_up_preview, risk_fn=_follow_up_risk, egress=True,
        send_intent=_follow_up_intent, default_mode="auto",
    ),
    ToolSpec(
        key="bgv.draft_provider_message", version=1, owner_epic=OWNER_EPIC, title="Draft a message to the provider",
        description="Draft a free-text email to the BGV provider contact on record. Always reviewed and sent by a person.",
        input_model=DraftIn, output_model=DraftOut, permissions=("bgv:follow_up", "email:send"), risk="high", scope=CHECK,
        handler=_draft, preview=_draft_preview, egress=True, send_intent=_follow_up_intent,
    ),
    ToolSpec(
        key="bgv.request_candidate_documents", version=1, owner_epic=OWNER_EPIC, title="Request documents from the candidate",
        description="Propose asking the candidate for more BGV documents (document keys of the BGV catalogue). Always approved by a person.",
        input_model=DocsIn, output_model=DocsOut, permissions=("bgv:manage",), risk="high", scope=CASE,
        handler=_docs, preview=_docs_preview,
    ),
    ToolSpec(
        key="bgv.post_case_summary", version=1, owner_epic=OWNER_EPIC, title="Post a pending summary",
        description="Post a short factual list of pending BGV items (documents, discrepancies, overdue checks) as an internal note for the case owner. No opinions about the candidate.",
        input_model=PostSummaryIn, output_model=PostSummaryOut, permissions=("note:write", "bgv:read"), risk="low", scope=CASE,
        handler=_post_summary, preview=_post_summary_preview, default_mode="auto", max_calls_per_run=1,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
