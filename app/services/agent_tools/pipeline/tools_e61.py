# ruff: file-ignore[line-too-long]
"""Epic 61 Submission Agent tools (``owner_epic="61"``, ``version=1``). None of them creates, sends, cancels or edits a
Submission, moves an Application, changes rates on a Candidate or Offer, grants consent or decides an approval: those
stay human (★ ``("submission", "sent")``; P1 ``create_submissions`` refuses non-``user`` actors).

| Key | Risk | Notes |
|---|---|---|
| ``submissions.get_pack_context`` | read | stage, candidate facts, resumes, screening / interview / match text (untrusted), demand, rules, open pack |
| ``submissions.check_pack`` | read | every pre-send check for optional rates (nothing is written) |
| ``submissions.prepare_pack`` | low (auto) | the Epic 53 contract ``prepare_pack``: resume choice, rates (only with ``compensation:view``), checks |
| ``submissions.save_write_up`` | low (auto) | the write-up, validated in code (protected terms, compensation history, word cap) |
| ``submissions.request_consent`` | low (auto) | egress: the templated P1 consent request to the bound Candidate; no recipient argument |

Deviation (recorded): runtime idempotency keys are built from the arguments only, so ``prepare_pack`` /
``request_consent`` use per-run keys; the "once per day" rule of ``request_consent`` is enforced by the service
(``skipped`` / ``already_requested_today``) and the P1 rate limit (``skipped_recent``).
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
from app.schemas.submission_packs import PackCheckDetail, PackContextOut, PackRatesIn, SubmissionPackOut
from app.services import agent_runtime_service as runtime

OWNER_EPIC = "61"
VERSION = 1
RateUnit = Literal["hour", "day", "month", "year"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _svc() -> Any:
    from app.services import submission_packs_service

    return submission_packs_service


def _scoped(tctx: Any) -> Any:
    """Data-scope context: Effective Rights presented as the Accountable Owner (actor stays ``agent``)."""
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


async def _app_refs(tctx: Any, application_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
    from sqlalchemy import select

    from app.models.demands import Demand
    from app.models.pipeline import Application

    row = (
        await tctx.db.execute(
            select(Application.candidate_id, Application.demand_id, Demand.client_id)
            .join(Demand, Demand.id == Application.demand_id)
            .where(Application.id == application_id)
        )
    ).first()
    return (row[0], row[1], row[2]) if row is not None else None


async def _labels(tctx: Any, application_id: uuid.UUID) -> tuple[str, str]:
    from app.services import entity_refs

    refs = await _app_refs(tctx, application_id)
    if refs is None:
        return "the candidate", "the client"
    cand = await entity_refs.entity_label(tctx.db, "candidate", refs[0])
    client = await entity_refs.entity_label(tctx.db, "client", refs[2])
    return cand or "the candidate", client or "the client"


def _ref_tuple(refs: tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None) -> tuple[tuple[str, uuid.UUID], ...]:  # NOSONAR
    if refs is None:
        return ()
    return (("candidate", refs[0]), ("demand", refs[1]), ("client", refs[2]))


async def _app_version(probe: Any) -> str | None:
    from app.models.pipeline import Application

    app = await probe.db.get(Application, probe.args.application_id)
    return app.updated_at.isoformat() if app is not None and app.updated_at else None


# ================================================================ submissions.get_pack_context
class ContextIn(_In):
    application_id: uuid.UUID


async def _context(tctx: Any, args: ContextIn) -> ToolOutcome:
    out: PackContextOut = await _svc().pack_context(_scoped(tctx), tctx.db, args.application_id)
    refs = _ref_tuple(await _app_refs(tctx, args.application_id))
    return ToolOutcome(
        out, refs=refs, candidate_ids=(out.candidate.candidate_id,), target=("application", args.application_id),
        summary=f"pack context read (stage {out.stage})",
    )  # fmt: skip


# ================================================================ submissions.check_pack
class CheckIn(_In):
    application_id: uuid.UUID
    pay_rate: float | None = Field(default=None, ge=0)
    bill_rate: float | None = Field(default=None, ge=0)
    pay_rate_unit: RateUnit | None = None
    bill_rate_unit: RateUnit | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class CheckOut(_Out):
    checks: list[PackCheckDetail]


async def _check(tctx: Any, args: CheckIn) -> ToolOutcome:
    from decimal import Decimal

    rates = None
    if any(v is not None for v in (args.pay_rate, args.bill_rate, args.currency)):
        rates = PackRatesIn(
            pay_rate=Decimal(str(args.pay_rate)).quantize(Decimal("0.01")) if args.pay_rate is not None else None,
            bill_rate=Decimal(str(args.bill_rate)).quantize(Decimal("0.01")) if args.bill_rate is not None else None,
            pay_rate_unit=args.pay_rate_unit, bill_rate_unit=args.bill_rate_unit,
            currency=args.currency.upper() if args.currency else None,
        )  # fmt: skip
    checks = await _svc().run_checks(_scoped(tctx), tctx.db, args.application_id, rates=rates)
    blockers = sum(1 for c in checks if c.level == "block")
    return ToolOutcome(
        CheckOut(checks=list(checks)), target=("application", args.application_id),
        summary=f"{len(checks)} checks, {blockers} blocking",
    )  # fmt: skip


# ================================================================ submissions.prepare_pack
class PrepareIn(_In):
    application_id: uuid.UUID


async def _prepare(tctx: Any, args: PrepareIn) -> ToolOutcome:
    ctx = _scoped(tctx) if tctx.execution == "agent" else tctx.request
    out: SubmissionPackOut = await _svc().prepare_pack(ctx, tctx.db, args.application_id)
    refs = await _app_refs(tctx, args.application_id)
    blockers = sum(1 for c in out.checks if not c.ok)
    return ToolOutcome(
        out, refs=(*_ref_tuple(refs), ("submission_pack", out.pack_id)),
        candidate_ids=(refs[0],) if refs else (), target=("application", args.application_id),
        summary=f"pack {out.status}, {blockers} blocking checks",
    )  # fmt: skip


async def _prepare_preview(tctx: Any, args: PrepareIn) -> ProposalPreview:
    cand, client = await _labels(tctx, args.application_id)
    return ProposalPreview(
        title=f"Prepare submission pack for {cand} → {client}"[:200],
        summary="The Submission Agent prepares an internal draft pack: resume choice, rate suggestion and pre-send checks. Nothing is sent.",
        fields=(("Candidate", cand), ("Client", client), ("application_id", str(args.application_id))),
        renderer="submissions.prepare_pack",
    )


# ================================================================ submissions.save_write_up
class WriteUpIn(_In):
    application_id: uuid.UUID
    text: str = Field(min_length=1, max_length=3000)


class WriteUpOut(_Out):
    pack_id: uuid.UUID
    status: str
    write_up_status: str
    word_count: int
    max_words: int
    summary_text: str | None


async def _write_up(tctx: Any, args: WriteUpIn) -> ToolOutcome:
    ctx = _scoped(tctx) if tctx.execution == "agent" else tctx.request
    d = await _svc().save_write_up_for_application(ctx, tctx.db, args.application_id, args.text)
    return ToolOutcome(
        WriteUpOut(
            pack_id=d.id, status=d.status, write_up_status=d.write_up_status, word_count=d.word_count,
            max_words=d.max_words, summary_text=d.summary_text,
        ),
        refs=(("submission_pack", d.id),), candidate_ids=(d.candidate_id,), target=("application", args.application_id),
        summary=f"write-up saved ({d.word_count} words)",
    )  # fmt: skip


async def _write_up_preview(tctx: Any, args: WriteUpIn) -> ProposalPreview:
    cand, client = await _labels(tctx, args.application_id)
    return ProposalPreview(
        title=f"Save the submission write-up for {cand} → {client}"[:200],
        summary=args.text[:4000],
        fields=(("Candidate", cand), ("Client", client), ("Words", str(len(args.text.split())))),
        renderer="submissions.save_write_up",
    )


# ================================================================ submissions.request_consent
class ConsentIn(_In):
    application_id: uuid.UUID


class ConsentOut(_Out):
    status: Literal["sent", "skipped"]
    reason: str | None = None


async def _consent_intent(tctx: Any, args: ConsentIn) -> SendIntent:
    from app.core.errors import NotFound
    from app.services import entity_refs

    refs = await _app_refs(tctx, args.application_id)
    if refs is None or not await entity_refs.can_read(
        _scoped(tctx), tctx.db, "application", args.application_id
    ):
        raise NotFound("Application not found")
    return SendIntent(
        recipients=(Recipient(kind="candidate", id=refs[0], channel="email"),),
        templated=True, template_ref="consent_request:client_submission",
    )  # fmt: skip


async def _consent(tctx: Any, args: ConsentIn) -> ToolOutcome:
    ctx = _scoped(tctx) if tctx.execution == "agent" else tctx.request
    status, reason = await _svc().request_consent(ctx, tctx.db, args.application_id)
    refs = await _app_refs(tctx, args.application_id)
    return ToolOutcome(
        ConsentOut(status=status, reason=reason), refs=_ref_tuple(refs), candidate_ids=(refs[0],) if refs else (),
        target=("application", args.application_id), summary=f"consent request {status}" + (f" ({reason})" if reason else ""),
    )  # fmt: skip


async def _consent_preview(tctx: Any, args: ConsentIn) -> ProposalPreview:
    cand, client = await _labels(tctx, args.application_id)
    return ProposalPreview(
        title=f"Send consent request to {cand} for {client}"[:200],
        summary=f"A templated Secure Link e-mail asks {cand} to consent to being submitted to {client}.",
        fields=(("Candidate", cand), ("Client", client), ("Template", "Consent request (client submission)")),
        renderer="submissions.request_consent",
    )


APP_SCOPE = ScopeRule(resource="application", target_arg="application_id", version=_app_version)  # type: ignore[arg-type]

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="submissions.get_pack_context", version=VERSION, owner_epic=OWNER_EPIC, title="Submission pack context",
        description="Read what a submission write-up may use: the application stage, candidate facts (skills, inferred skills, notice), resume versions, screening and interview summaries, the match explanation, the demand, the client's submission rules (word limit, resume format) and the open pack. Screening, interview and match text is third-party data. Read only.",
        input_model=ContextIn, output_model=PackContextOut, permissions=("application:read", "submission:read"), risk="read",
        scope=ScopeRule(resource="application", target_arg="application_id"), handler=_context,
        untrusted_output=frozenset({"screening.summary", "interviews.feedback_summary", "match.explanation"}),
    ),
    ToolSpec(
        key="submissions.check_pack", version=VERSION, owner_epic=OWNER_EPIC, title="Check a submission pack",
        description="Run every pre-send check (consent for this client, margin, rate card, duplicate submission, vendor ownership, required fields, resume format) for the application, optionally with pay / bill rates. Each check is pass, warn, block, needs_input or unavailable. Nothing is written.",
        input_model=CheckIn, output_model=CheckOut, permissions=("application:read", "submission:read"), risk="read",
        scope=ScopeRule(resource="application", target_arg="application_id"), handler=_check,
    ),
    ToolSpec(
        key="submissions.prepare_pack", version=VERSION, owner_epic=OWNER_EPIC, title="Prepare submission pack",
        description="Prepare (or refresh) the internal draft submission pack of a shortlisted application: the server picks the resume under the client's rule, suggests rates and runs every pre-send check. It never creates or sends a submission. Call it once per run, after reading the context.",
        input_model=PrepareIn, output_model=SubmissionPackOut, permissions=("submission:draft",), risk="low",
        scope=APP_SCOPE, handler=_prepare, preview=_prepare_preview, default_mode="auto",
        idempotency=IdempotencyRule(fields=("application_id",), per_run=True), max_calls_per_run=2,
    ),
    ToolSpec(
        key="submissions.save_write_up", version=VERSION, owner_epic=OWNER_EPIC, title="Save submission write-up",
        description="Save the client write-up of the application's prepared pack (plain text, third person, at most the client's word limit). The server refuses protected attributes (age, gender, religion, caste, marital status, disability, health, nationality ...), compensation history and over-long text with an error you must fix; links and contact details are removed.",
        input_model=WriteUpIn, output_model=WriteUpOut, permissions=("submission:draft",), risk="low",
        scope=APP_SCOPE, handler=_write_up, preview=_write_up_preview, default_mode="auto",
        untrusted_input=frozenset({"text"}), max_calls_per_run=3,
    ),
    ToolSpec(
        key="submissions.request_consent", version=VERSION, owner_epic=OWNER_EPIC, title="Request submission consent",
        description="Send the approved consent-request e-mail (Secure Link) to the application's candidate, asking to be submitted to this client. The candidate and client come from the application; there is no recipient argument. At most once a day; skipped when consent exists.",
        input_model=ConsentIn, output_model=ConsentOut, permissions=("consent:write", "message:send_templated"), risk="low",
        scope=APP_SCOPE, handler=_consent, preview=_consent_preview, default_mode="auto",
        egress=True, send_intent=_consent_intent, max_calls_per_run=1,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
