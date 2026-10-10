# ruff: noqa: E501
"""Screening Agent tools (Epic 57 Part B; ``owner_epic="57"``, ``version=1``).

Every tool takes the ``conversation_id`` and nothing else that names a person: the conversation's Candidate is the
only recipient (no recipient argument exists). The conversation must belong to the run subject (the Application), else
``not_found``. Texts to the Candidate are server-rendered from approved content (questions, clarification templates,
the disclosure, reminders, the hand-off acknowledgement); ``screening.reply_grounded`` is the only model text and must
cite published posting fields (commitment validator). In-conversation sends declare ``SendIntent(grounded=True)`` once
the Candidate accepted the conversation, so they stay ``auto`` in a tainted run (FR-274, coordinator decision 3).

| Key | Risk | Default after burn-in |
|---|---|---|
| ``screening.questionnaire`` | read | auto |
| ``screening.invite`` / ``ask`` / ``clarify`` / ``reply_grounded`` / ``extract_answers`` / ``save_summary`` / ``reminder`` / ``handoff`` | low | auto |
| ``screening.propose_candidate_updates`` | high | Proposal only |

★ (never tools): the screening outcome, stage moves, reject (``FORBIDDEN_ACTIONS``).
"""

import uuid
from typing import Any, Literal

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
from app.core.errors import ValidationFailed
from app.services import screening_agent_service as svc

OWNER_EPIC = "57"
VERSION = 1
ACCEPTED = ("consented", "in_progress")
SCOPE = ScopeRule(resource="screening_conversation", target_arg="conversation_id", cross_subject=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _conv(tctx: Any, conversation_id: uuid.UUID, *, write: bool = False) -> Any:
    return await svc.ops.load(tctx.db, conversation_id, tctx.subject, write=write)


def _outcome(data: BaseModel, conv: Any, summary: str) -> ToolOutcome:
    return ToolOutcome(
        data, refs=(("screening_conversation", conv.id), ("candidate", conv.candidate_id)),
        candidate_ids=(conv.candidate_id,), target=("screening_conversation", conv.id), summary=summary,
    )  # fmt: skip


async def _intent(
    tctx: Any,
    conversation_id: uuid.UUID,
    *,
    templated: bool,
    citations: tuple[str, ...],
    text: str = "",
    template_ref: str | None = None,
    force_grounded: bool = False,
) -> SendIntent:
    conv = await _conv(tctx, conversation_id)
    grounded = force_grounded or (conv.status in ACCEPTED and bool(citations))
    return SendIntent(
        recipients=(Recipient(kind="candidate", id=conv.candidate_id, channel=svc.ops.recipient_channel(conv)),),  # type: ignore[arg-type]
        templated=templated, template_ref=template_ref, grounded=grounded, conversation_id=conv.id if grounded else None,
        citations=citations if grounded else (), text_for_checks=text,
    )  # fmt: skip


def _preview(title: str, summary_fn: Any = None) -> Any:
    async def preview(tctx: Any, args: Any) -> ProposalPreview:  # noqa: ARG001
        fields: list[tuple[str, str]] = [("Conversation", str(args.conversation_id))]
        summary = summary_fn(args) if summary_fn is not None else title
        return ProposalPreview(title=title, summary=summary[:4000], fields=tuple(fields))

    return preview


# ================================================================ screening.questionnaire (read)
class QuestionView(_Out):
    id: uuid.UUID
    label: str
    type: str
    unit: str | None = None
    options: list[str] | None = None
    required: bool
    knockout: dict[str, Any] | None = None


class AnsweredView(_Out):
    question_id: uuid.UUID
    normalized: dict[str, Any] | None
    confidence: float


class RequirementView(_Out):
    label: str
    must_have: bool


class QuestionnaireOut(_Out):
    version_id: uuid.UUID
    questions: list[QuestionView]
    answered: list[AnsweredView]
    clarification_keys: dict[str, list[str]]
    demand_requirements: list[RequirementView]


async def _questionnaire(tctx: Any, args: _In) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id)
    data = await svc.ops.questionnaire_view(tctx.db, conv)
    return _outcome(QuestionnaireOut.model_validate(data), conv, f"{len(data['questions'])} questions")


# ================================================================ screening.invite
class InviteOut(_Out):
    turn_seq: int
    delivered: bool


async def _invite(tctx: Any, args: _In) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.invite(tctx.request, tctx.db, conv, tctx.run_id)
    return _outcome(InviteOut(**data), conv, "invitation sent" if data["delivered"] else "invitation stored")


async def _invite_intent(tctx: Any, args: _In) -> SendIntent:
    return await _intent(
        tctx, args.conversation_id, templated=True, citations=(), template_ref="ai_screening_invite"
    )


# ================================================================ screening.ask
class AskIn(_In):
    question_ids: list[uuid.UUID] = Field(min_length=1, max_length=3)


async def _ask(tctx: Any, args: AskIn) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.ask(tctx.request, tctx.db, conv, list(dict.fromkeys(args.question_ids)), tctx.run_id)
    return _outcome(InviteOut(**data), conv, f"asked {len(args.question_ids)} question(s)")


async def _ask_intent(tctx: Any, args: AskIn) -> SendIntent:
    return await _intent(
        tctx,
        args.conversation_id,
        templated=True,
        citations=tuple(f"question:{q}" for q in args.question_ids),
    )


# ================================================================ screening.clarify
class ClarifyIn(_In):
    question_id: uuid.UUID
    template_key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")


class TurnSeqOut(_Out):
    turn_seq: int


async def _clarify(tctx: Any, args: ClarifyIn) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.clarify(
        tctx.request, tctx.db, conv, args.question_id, args.template_key, tctx.run_id
    )
    return _outcome(TurnSeqOut(**data), conv, "clarification sent")


async def _clarify_intent(tctx: Any, args: ClarifyIn) -> SendIntent:
    return await _intent(
        tctx, args.conversation_id, templated=True, citations=(f"clarification:{args.template_key}",)
    )


# ================================================================ screening.reply_grounded
class GroundedIn(_In):
    text: str = Field(min_length=1, max_length=600)
    cited_fields: list[str] = Field(min_length=1, max_length=5)


async def _grounded(tctx: Any, args: GroundedIn) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.reply_grounded(
        tctx.request, tctx.db, conv, args.text, list(dict.fromkeys(args.cited_fields)), tctx.run_id
    )
    return _outcome(TurnSeqOut(**data), conv, "grounded answer sent")


async def _grounded_intent(tctx: Any, args: GroundedIn) -> SendIntent:
    return await _intent(
        tctx, args.conversation_id, templated=False, citations=tuple(f"posting:{f}" for f in args.cited_fields),
        text=args.text, force_grounded=True,
    )  # fmt: skip


# ================================================================ screening.extract_answers
class ExtractedView(_Out):
    question_id: uuid.UUID
    normalized: dict[str, Any] | None
    confidence: float
    flags: list[str]


class ExtractOut(_Out):
    answered: list[ExtractedView]
    unanswered_required: list[uuid.UUID]


async def _extract(tctx: Any, args: _In) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.extract(tctx.request, tctx.db, conv, tctx.run_id)
    return _outcome(ExtractOut.model_validate(data), conv, f"{len(data['answered'])} answers extracted")


# ================================================================ screening.save_summary
class SummaryIn(_In):
    summary: str = Field(min_length=1, max_length=3000)
    gaps: list[str] = Field(default_factory=list, max_length=20)
    suggested_next: Literal["proceed", "hold"] | None = None
    final: bool = False


class StatusOut(_Out):
    status: str


async def _summary(tctx: Any, args: SummaryIn) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    gaps = [g.strip()[:300] for g in args.gaps if g.strip()]
    data = await svc.ops.save_summary(
        tctx.request, tctx.db, conv, args.summary.strip(), gaps, args.suggested_next, args.final, tctx.run_id
    )
    return _outcome(StatusOut(**data), conv, "summary saved" + (" (final)" if args.final else ""))


# ================================================================ screening.reminder
class ReminderOut(_Out):
    sent: bool


async def _reminder(tctx: Any, args: _In) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.reminder(tctx.request, tctx.db, conv, tctx.run_id)
    return _outcome(ReminderOut(**data), conv, "reminder sent" if data["sent"] else "reminder stored")


async def _reminder_intent(tctx: Any, args: _In) -> SendIntent:
    return await _intent(
        tctx, args.conversation_id, templated=True, citations=(), template_ref="screening_reminder"
    )


# ================================================================ screening.handoff
class HandoffIn(_In):
    reason: Literal[
        "candidate_request", "compensation", "ungrounded_question", "language", "model_failure", "other"
    ]
    note: str | None = Field(default=None, max_length=500)


class HandoffOut(_Out):
    task_id: uuid.UUID | None


async def _handoff(tctx: Any, args: HandoffIn) -> ToolOutcome:
    conv = await _conv(tctx, args.conversation_id, write=True)
    data = await svc.ops.handoff(tctx.request, tctx.db, conv, args.reason, args.note, tctx.run_id)
    return _outcome(HandoffOut(**data), conv, f"handed off ({args.reason})")


async def _handoff_intent(tctx: Any, args: HandoffIn) -> SendIntent:
    return await _intent(
        tctx,
        args.conversation_id,
        templated=True,
        citations=("template:handoff_ack",),
        template_ref="handoff_ack",
    )


# ================================================================ screening.propose_candidate_updates (high)
class UpdatesOut(_Out):
    applied: list[str]


async def _updates(tctx: Any, args: _In) -> ToolOutcome:
    if tctx.execution == "agent":  # defence in depth: risk 'high' never reaches auto
        raise ValidationFailed("Candidate updates need a person's approval", code="proposal_required")
    conv = await _conv(tctx, args.conversation_id)
    applied = await svc.ops.apply_candidate_updates(tctx.request, tctx.db, conv)
    return _outcome(UpdatesOut(applied=applied), conv, f"{len(applied)} candidate field(s) updated")


async def _updates_preview(tctx: Any, args: _In) -> ProposalPreview:
    conv = await _conv(tctx, args.conversation_id)
    fields = await svc.ops.candidate_update_fields(tctx.db, conv)
    if not fields:
        raise ValidationFailed("There are no candidate updates to propose", code="no_proposal")
    return ProposalPreview(
        title="Update candidate fields from the AI screening",
        summary="\n".join(f"{f['field']}: {f.get('current')} -> {f.get('proposed')}" for f in fields)[:4000],
        fields=tuple((str(f["field"]), str(f.get("proposed"))) for f in fields),
    )


async def _updates_version(probe: Any) -> str:
    conv = await svc.ops.load(probe.db, probe.args.conversation_id, probe.tctx.subject)
    return await svc.ops.updates_version(probe.db, conv)


# ================================================================ registration
_SPECS = (
    ToolSpec(
        key="screening.questionnaire",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Read the screening questionnaire",
        description="The approved questionnaire of this conversation: question ids, labels, types, units, required flags, knockout flags (for the recruiter only), the answers so far, approved clarification template keys per type and the demand's must-have skills.",
        input_model=_In,
        output_model=QuestionnaireOut,
        permissions=("screening:conduct",),
        risk="read",
        scope=SCOPE,
        handler=_questionnaire,
    ),
    ToolSpec(
        key="screening.invite",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Send the screening invitation",
        description="Sends the templated invitation with the AI-assistant disclosure on the conversation channel. Once per conversation, before anything else.",
        input_model=_In,
        output_model=InviteOut,
        permissions=("message:send_templated",),
        risk="low",
        scope=SCOPE,
        handler=_invite,
        egress=True,
        send_intent=_invite_intent,
        preview=_preview("Send the AI screening invitation"),
        idempotency=IdempotencyRule(fields=("conversation_id",), per_run=False),
        default_mode="auto",
        max_calls_per_run=1,
    ),
    ToolSpec(
        key="screening.ask",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Ask screening questions",
        description="Sends 1-3 questions of the approved questionnaire by id; the server renders the text. Refused before consent (consent_pending), for unknown ids (unknown_question) and outside the WhatsApp window (window_closed).",
        input_model=AskIn,
        output_model=InviteOut,
        permissions=("screening:conduct",),
        risk="low",
        scope=SCOPE,
        handler=_ask,
        egress=True,
        send_intent=_ask_intent,
        preview=_preview(
            "Ask screening questions", lambda a: f"Ask {len(a.question_ids)} approved question(s)"
        ),
        default_mode="auto",
    ),
    ToolSpec(
        key="screening.clarify",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Ask for a clarification",
        description="Asks the candidate to clarify one answer with an approved clarification template key for the question's type.",
        input_model=ClarifyIn,
        output_model=TurnSeqOut,
        permissions=("screening:conduct",),
        risk="low",
        scope=SCOPE,
        handler=_clarify,
        egress=True,
        send_intent=_clarify_intent,
        preview=_preview("Ask for a clarification", lambda a: f"Clarification template {a.template_key}"),
        default_mode="auto",
    ),
    ToolSpec(
        key="screening.reply_grounded",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Answer a job question from the posting",
        description="Answers the candidate's question about the job using only published posting fields (postings.get), citing 1-5 field keys. No promises, outcomes, interviews or amounts (refused as invalid). Max 600 characters.",
        input_model=GroundedIn,
        output_model=TurnSeqOut,
        permissions=("screening:conduct",),
        risk="low",
        scope=SCOPE,
        handler=_grounded,
        egress=True,
        send_intent=_grounded_intent,
        untrusted_input=frozenset({"text"}),
        preview=_preview("Answer a job question", lambda a: a.text),
        default_mode="auto",
    ),
    ToolSpec(
        key="screening.extract_answers",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Record the candidate's answers",
        description="Extracts structured answers (value, confidence, flags) from the conversation so far and returns the required questions still unanswered.",
        input_model=_In,
        output_model=ExtractOut,
        permissions=("screening:conduct",),
        risk="low",
        scope=SCOPE,
        handler=_extract,
        preview=_preview("Record the candidate's answers"),
        default_mode="auto",
    ),
    ToolSpec(
        key="screening.save_summary",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Save the screening summary",
        description="Saves a factual summary (answers and gaps against the demand only; never tone, personality, emotion or protected traits -> summary_language). final=true completes the conversation for recruiter review.",
        input_model=SummaryIn,
        output_model=StatusOut,
        permissions=("screening:conduct",),
        risk="low",
        scope=SCOPE,
        handler=_summary,
        preview=_preview("Save the screening summary", lambda a: a.summary),
        default_mode="auto",
    ),
    ToolSpec(
        key="screening.reminder",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Send a screening reminder",
        description="Sends one templated reminder (at most the tenant maximum; never in quiet hours).",
        input_model=_In,
        output_model=ReminderOut,
        permissions=("message:send_templated",),
        risk="low",
        scope=SCOPE,
        handler=_reminder,
        egress=True,
        send_intent=_reminder_intent,
        preview=_preview("Send a screening reminder"),
        default_mode="auto",
        max_calls_per_run=1,
    ),
    ToolSpec(
        key="screening.handoff",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Hand the conversation to the recruiter",
        description="Ends the AI part: sends the templated acknowledgement 'A recruiter will get back to you on that.', creates a Task for the recruiter and marks the conversation handed off. Use for pay negotiation, outcomes, interviews, ungrounded or personal questions, other languages.",
        input_model=HandoffIn,
        output_model=HandoffOut,
        permissions=("task:create",),
        risk="low",
        scope=SCOPE,
        handler=_handoff,
        egress=True,
        send_intent=_handoff_intent,
        preview=_preview(
            "Hand the screening to the recruiter", lambda a: f"Reason: {a.reason}. {a.note or ''}"
        ),
        default_mode="auto",
        max_calls_per_run=1,
    ),
    ToolSpec(
        key="screening.propose_candidate_updates",
        version=VERSION,
        owner_epic=OWNER_EPIC,
        title="Propose candidate field updates",
        description="Proposes updating candidate fields (notice period, expected CTC, ...) from the screening answers. Always a Proposal for a person to approve.",
        input_model=_In,
        output_model=UpdatesOut,
        permissions=("candidate:update",),
        risk="high",
        scope=ScopeRule(
            resource="screening_conversation",
            target_arg="conversation_id",
            cross_subject=True,
            version=_updates_version,
        ),
        handler=_updates,
        preview=_updates_preview,
        max_calls_per_run=1,
    ),
)

for _spec in _SPECS:
    register_tool(_spec)
