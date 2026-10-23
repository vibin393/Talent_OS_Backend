# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Submission packs (Epic 61, FR-305..FR-307). Fills the Epic 53 contract stub ``prepare_pack`` (signature pinned by
``tests/contracts/signatures.json``).

* ``prepare_pack`` -- deterministic: picks the resume under the Client rule, suggests rates (only when the principal may
  see compensation), runs every pre-send check and upserts the single ``open`` pack of the Application
  (``version += 1``; an existing write-up is kept unless the resume changed). It never creates a Submission.
* ``save_write_up`` -- the only model-written field, validated in code (``writeup.clean``).
* Human edits (``set_rates`` / ``patch``), ``mark_opened`` (the P1 dialog was pre-filled), ``discard``.
* ``link_submission`` -- the Submission a person created from the pack: lifecycle ``used`` + the diff of their changes.
* ``recheck_for_candidate`` -- consent changed: re-run the checks of the candidate's open packs (no model call).
* ``pack_context`` (the agent's read tool), ``request_consent`` (templated P1 consent request), ``metrics``.
"""

import statistics
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Scope, can_view_compensation, check, forbidden
from app.core.sequences import next_human_id
from app.events.submission_packs import SubmissionPackReady, SubmissionPackUsed
from app.models.candidates import Candidate, CandidateResume, CandidateSkill, Skill, TenantSkill
from app.models.demands import Demand
from app.models.pipeline import Application, Submission
from app.models.submission_packs import SubmissionPack
from app.schemas.submission_packs import (
    ContextCandidate,
    ContextDemand,
    ContextInterview,
    ContextMatch,
    ContextPack,
    ContextResume,
    ContextRules,
    ContextScreening,
    PackCheckDetail,
    PackContextOut,
    PackDetailOut,
    PackMetricsOut,
    PackPage,
    PackPatchIn,
    PackRatesIn,
    PackSummaryOut,
    SubmissionPackOut,
)
from app.services.submission_packs import checks as checks_mod
from app.services.submission_packs import writeup
from app.services.submission_packs.checks import (
    Rates,
    evaluate,
    load_facts,
    missing_of,
    resume_choice,
    to_stub,
)
from app.services.submission_packs.common import (
    CONSENT_PURPOSE,
    DRAFT,
    PACK_READ,
    READ,
    SEQUENCE_KEY,
    SEQUENCE_PREFIX,
    SUBMIT,
    application_for,
    application_for_any,
    can_comp,
    conflict,
    now,
    open_pack,
    principal,
    tenant_of,
    word_count,
)
from app.services.submission_packs.diff import pack_diff

RATE_UNITS = ("hour", "day", "month", "year")
# Candidate ``expected_ctc_unit`` (``models.candidates.CTC_UNITS``) -> P1 rate unit.
CTC_TO_RATE_UNIT = {"annual": "year", "monthly": "month", "daily": "day", "hourly": "hour"}
NOTIFY_STATUSES = ("ready", "blocked")


# ================================================================ helpers
def _status(checks: tuple[PackCheckDetail, ...] | list[PackCheckDetail], summary: str | None) -> str:
    if any(c.level == "block" for c in checks):
        return "blocked"
    return "ready" if (summary or "").strip() else "draft"


def _prepared_by(ctx: RequestContext) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    if getattr(ctx, "actor_kind", None) == "agent":
        return "agent", getattr(ctx, "actor_id", None), getattr(ctx, "agent_run_id", None)
    p = principal(ctx)
    if p is not None and p.type == "user":
        return "user", p.id, None
    return "system", None, None


def _rates_of(pack: SubmissionPack | None) -> Rates:
    if pack is None:
        return Rates()
    return Rates(pack.pay_rate, pack.bill_rate, pack.pay_rate_unit, pack.bill_rate_unit, pack.currency)


def _suggest_rates(demand: Demand, candidate: Candidate) -> Rates:
    """Demand bill rate / unit / currency; pay = the candidate's expected compensation (same currency, converted to the
    demand unit) capped at the Demand's ``pay_rate_max``."""
    from app.services.pipeline import margin as margin_calc

    unit = demand.rate_unit if demand.rate_unit in RATE_UNITS else None
    currency = demand.currency
    pay: Decimal | None = None
    ctc_unit = (
        CTC_TO_RATE_UNIT.get(candidate.expected_ctc_unit or "", unit) if candidate.expected_ctc_unit else unit
    )
    if (
        unit is not None
        and candidate.expected_ctc_amount is not None
        and (candidate.expected_ctc_currency or currency) == currency
        and ctc_unit in RATE_UNITS
    ):
        pay = margin_calc.q2(margin_calc.normalize(Decimal(candidate.expected_ctc_amount), ctc_unit, unit))
        if demand.pay_rate_max is not None:
            pay = min(pay, Decimal(demand.pay_rate_max))
    bill = Decimal(demand.bill_rate) if demand.bill_rate is not None else None
    return Rates(pay_rate=pay, bill_rate=bill, pay_rate_unit=unit, bill_rate_unit=unit, currency=currency)


async def _validate_template(db: AsyncSession, template_id: uuid.UUID) -> None:
    """``client_template_id`` is reserved for a client-specific profile layout: it must be an active document template
    of kind ``client_profile`` (none exists in P1-P3, so any value is refused); validated and otherwise ignored."""
    from app.models.documents import DocumentTemplate

    row = await db.get(DocumentTemplate, template_id)
    if row is None or row.archived_at is not None or row.kind != "client_profile":
        raise ValidationFailed(
            "Unknown client profile template",
            code="client_template_unknown",
            details={"client_template_id": str(template_id)},
        )


def _emit_ready(db: AsyncSession, pack: SubmissionPack) -> None:
    emit(
        db,
        SubmissionPackReady(
            tenant_id=pack.tenant_id, pack_id=pack.id, application_id=pack.application_id, demand_id=pack.demand_id,
            client_id=pack.client_id, status=pack.status, blocker_count=int(pack.blocker_count),
            agent_run_id=pack.agent_run_id,
        ),
    )  # fmt: skip


def _apply_checks(pack: SubmissionPack, result: tuple[PackCheckDetail, ...]) -> bool:
    """Store the checks and derived fields; True when the status changed."""
    before = pack.status
    pack.checks_detail = [c.model_dump(mode="json") for c in result]
    pack.blocker_count = sum(1 for c in result if c.level == "block")
    pack.warning_count = sum(1 for c in result if c.level == "warn")
    pack.missing = list(missing_of(result, has_write_up=bool((pack.summary_text or "").strip())))
    pack.checked_at = now()
    pack.status = _status(result, pack.summary_text)
    return pack.status != before


def stub_out(pack: SubmissionPack) -> SubmissionPackOut:
    checks = [PackCheckDetail.model_validate(c) for c in pack.checks_detail or []]
    return SubmissionPackOut(
        pack_id=pack.id, application_id=pack.application_id, demand_id=pack.demand_id, client_id=pack.client_id,
        status=pack.status, checks=to_stub(checks), document_ids=tuple(pack.document_ids or ()),
        summary_text=pack.summary_text or "", missing=tuple(pack.missing or ()),
    )  # fmt: skip


# ================================================================ prepare (contract stub)
async def prepare(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    client_template_id: uuid.UUID | None = None,
    write_up_failed: bool = False,
) -> SubmissionPack:
    app = await application_for_any(ctx, db, application_id, (DRAFT, SUBMIT))
    if client_template_id is not None:
        await _validate_template(db, client_template_id)
    facts = await load_facts(db, app)
    if facts.stage != "shortlisted" and (await open_pack(db, app.id)) is None:
        raise conflict(
            "not_shortlisted", "Only shortlisted applications get a submission pack", stage=facts.stage
        )
    pack = await open_pack(db, app.id, for_update=True)
    comp = can_comp(ctx)
    rates = _rates_of(pack)
    if rates.pay_rate is None and rates.bill_rate is None:
        rates = _suggest_rates(facts.demand, facts.candidate) if comp else Rates()
    kind = resume_choice(facts.rules, facts.resume)
    resume = facts.resume
    created = pack is None
    if pack is None:
        pack = SubmissionPack(
            id=uuid.uuid4(), tenant_id=tenant_of(db), human_id=await next_human_id(db, SEQUENCE_KEY, SEQUENCE_PREFIX, width=6),
            application_id=app.id, candidate_id=app.candidate_id, demand_id=app.demand_id, client_id=facts.demand.client_id,
            status="draft", lifecycle="open", version=1, write_up_status="pending", checks_detail=[], missing=[],
            document_ids=[], blocker_count=0, warning_count=0, lock_version=1, prepared_by_kind="system",
        )  # fmt: skip
        db.add(pack)
    else:
        pack.version = int(pack.version or 1) + 1
        pack.lock_version = int(pack.lock_version or 1) + 1
        if (resume.id if resume else None) != pack.resume_id:
            pack.summary_text = None
            pack.write_up_status = "pending"
    pack.resume_kind = kind
    pack.resume_id = resume.id if resume else None
    pack.resume_version = resume.version if resume else None
    pack.document_ids = [resume.file_id] if resume is not None and kind is not None else []
    pack.pay_rate, pack.bill_rate = rates.pay_rate, rates.bill_rate
    pack.pay_rate_unit, pack.bill_rate_unit, pack.currency = (
        rates.pay_rate_unit,
        rates.bill_rate_unit,
        rates.currency,
    )
    if not pack.email_subject:
        pack.email_subject = f"Candidate submission for {facts.demand.title}"[:300]
    if write_up_failed and not (pack.summary_text or "").strip():
        pack.write_up_status = "failed"
    pack.prepared_by_kind, pack.prepared_by_id, run_id = _prepared_by(ctx)
    pack.agent_run_id = run_id or (pack.agent_run_id if pack.prepared_by_kind == "system" else None)
    before = None if created else pack.status
    result = await evaluate(ctx, db, facts, rates)
    _apply_checks(pack, result)
    await db.flush()
    audit.record(
        "submission_pack.prepared", "submission_pack", pack.id, session=db, version=pack.version, status=pack.status,
        application_id=str(app.id), prepared_by=pack.prepared_by_kind,
    )  # fmt: skip
    if created or pack.status != before:
        _emit_ready(db, pack)
    return pack


async def prepare_pack(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    client_template_id: uuid.UUID | None = None,
) -> SubmissionPackOut:
    """Prepare (or refresh) the client submission pack of an application."""
    pack = await prepare(ctx, db, application_id, client_template_id=client_template_id)
    return stub_out(pack)


# ================================================================ checks (read)
async def run_checks(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, *, rates: PackRatesIn | None = None
) -> tuple[PackCheckDetail, ...]:
    app = await application_for(ctx, db, application_id, PACK_READ)
    facts = await load_facts(db, app)
    pack = await open_pack(db, app.id)
    r = Rates.of(rates) if rates is not None else _rates_of(pack)
    result = await evaluate(ctx, db, facts, r)  # read only: nothing is written
    p = principal(ctx)
    return tuple(checks_mod.mask_details(list(result), may_see_amounts=p is None or can_view_compensation(p)))


async def recheck(ctx: RequestContext, db: AsyncSession, pack: SubmissionPack) -> bool:
    """Re-run the checks of an open pack (consent changes, refresh); True when the status changed."""
    app = await db.get(Application, pack.application_id)
    if app is None or pack.lifecycle != "open":
        return False
    facts = await load_facts(db, app)
    changed = _apply_checks(pack, await evaluate(ctx, db, facts, _rates_of(pack)))
    await db.flush()
    if changed:
        _emit_ready(db, pack)
    return changed


async def recheck_for_candidate(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> int:
    packs = (
        await db.execute(
            select(SubmissionPack).where(
                SubmissionPack.candidate_id == candidate_id, SubmissionPack.lifecycle == "open"
            )
        )
    ).scalars()
    return sum([int(await recheck(ctx, db, p)) for p in list(packs)])


# ================================================================ reads
async def _pack_for(
    ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID, permission: str = PACK_READ
) -> SubmissionPack:
    pack = await db.get(SubmissionPack, pack_id)
    if pack is None:
        raise NotFound("Submission pack not found")
    try:
        await application_for(ctx, db, pack.application_id, PACK_READ)
    except NotFound:
        raise NotFound("Submission pack not found") from None
    if permission != PACK_READ:
        await application_for(ctx, db, pack.application_id, permission, missing_is_404=False)
    return pack


async def detail(ctx: RequestContext, db: AsyncSession, pack: SubmissionPack) -> PackDetailOut:
    from sqlalchemy import inspect as sa_inspect

    from app.services import entity_refs
    from app.services.submission_packs.rules import effective_rules

    if sa_inspect(pack).unloaded:  # server defaults / onupdate columns expired by the last flush
        await db.refresh(pack)
    p = principal(ctx)
    see = p is None or can_view_compensation(p)
    rules = await effective_rules(db, pack.client_id)
    latest = await checks_mod.latest_resume(db, pack.candidate_id)
    resume = await db.get(CandidateResume, pack.resume_id) if pack.resume_id else None
    sub_human = None
    if pack.submission_id is not None:
        sub = await db.get(Submission, pack.submission_id)
        sub_human = sub.human_id if sub is not None else None
    demand = await db.get(Demand, pack.demand_id)
    checks = checks_mod.mask_details(
        [PackCheckDetail.model_validate(c) for c in pack.checks_detail or []], may_see_amounts=see
    )

    def money(v: Decimal | None) -> str | None:
        return None if v is None or not see else f"{Decimal(v):.2f}"

    changes = pack.user_changes
    if changes and not see:
        changes = {**changes, "before": {}, "after": {}}
    return PackDetailOut(
        id=pack.id, human_id=pack.human_id, application_id=pack.application_id, candidate_id=pack.candidate_id,
        candidate_name=await entity_refs.entity_label(db, "candidate", pack.candidate_id), demand_id=pack.demand_id,
        demand_title=demand.title if demand is not None else None, client_id=pack.client_id,
        client_name=await entity_refs.entity_label(db, "client", pack.client_id), status=pack.status,
        lifecycle=pack.lifecycle, version=pack.version, resume_kind=pack.resume_kind, resume_id=pack.resume_id,
        resume_version=pack.resume_version, resume_uploaded_at=resume.created_at if resume is not None else None,
        resume_stale=pack.lifecycle == "open" and latest is not None and latest.id != pack.resume_id,
        document_ids=list(pack.document_ids or []), summary_text=pack.summary_text, write_up_status=pack.write_up_status,
        word_count=word_count(pack.summary_text), max_words=rules.write_up_max_words,
        pay_rate=money(pack.pay_rate), bill_rate=money(pack.bill_rate), currency=pack.currency,
        pay_rate_unit=pack.pay_rate_unit, bill_rate_unit=pack.bill_rate_unit, rates_hidden=not see,
        email_subject=pack.email_subject, checks=checks, missing=list(pack.missing or []),
        blocker_count=pack.blocker_count, warning_count=pack.warning_count, checked_at=pack.checked_at,
        consent_request_sent_at=pack.consent_request_sent_at, prepared_by_kind=pack.prepared_by_kind,
        prepared_by_id=pack.prepared_by_id, agent_run_id=pack.agent_run_id, opened_in_dialog_at=pack.opened_in_dialog_at,
        submission_id=pack.submission_id, submission_human_id=sub_human, used_at=pack.used_at, user_changes=changes,
        discarded_reason=pack.discarded_reason, lock_version=pack.lock_version, created_at=pack.created_at,
        updated_at=pack.updated_at,
    )  # fmt: skip


async def get_detail(ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID) -> PackDetailOut:
    return await detail(ctx, db, await _pack_for(ctx, db, pack_id))


async def get_open_pack(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> PackDetailOut | None:
    """The open pack, else the latest used / discarded one (the panel shows "Sent as ..."); None when there is none."""
    await application_for(ctx, db, application_id, PACK_READ)
    pack = await open_pack(db, application_id)
    if pack is None:
        pack = (
            await db.execute(
                select(SubmissionPack)
                .where(SubmissionPack.application_id == application_id)
                .order_by(SubmissionPack.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return await detail(ctx, db, pack) if pack is not None else None


async def list_packs(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
    lifecycle: str | None = None,
    owner_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 25,
) -> PackPage:
    from app.services import entity_refs
    from app.services.pipeline.common import visible_applications

    p = principal(ctx)
    stmt = select(SubmissionPack, Application.owner_id).join(
        Application, Application.id == SubmissionPack.application_id
    )
    if p is not None:
        visible = visible_applications(p, PACK_READ).with_only_columns(Application.id)
        stmt = stmt.where(SubmissionPack.application_id.in_(visible))
    if client_id is not None:
        stmt = stmt.where(SubmissionPack.client_id == client_id)
    if status:
        stmt = stmt.where(SubmissionPack.status == status)
    if lifecycle:
        stmt = stmt.where(SubmissionPack.lifecycle == lifecycle)
    if owner_id is not None:
        stmt = stmt.where(Application.owner_id == owner_id)
    if date_from is not None:
        stmt = stmt.where(SubmissionPack.created_at >= datetime.combine(date_from, time.min, UTC))
    if date_to is not None:
        stmt = stmt.where(
            SubmissionPack.created_at < datetime.combine(date_to + timedelta(days=1), time.min, UTC)
        )
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(SubmissionPack.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    items: list[PackSummaryOut] = []
    for pack, owner in rows:
        demand = await db.get(Demand, pack.demand_id)
        user = await entity_refs.user(db, owner) if owner else None
        items.append(
            PackSummaryOut(
                id=pack.id, human_id=pack.human_id, application_id=pack.application_id, candidate_id=pack.candidate_id,
                candidate_name=await entity_refs.entity_label(db, "candidate", pack.candidate_id), demand_id=pack.demand_id,
                demand_title=demand.title if demand is not None else None, client_id=pack.client_id,
                client_name=await entity_refs.entity_label(db, "client", pack.client_id), status=pack.status,
                lifecycle=pack.lifecycle, blocker_count=pack.blocker_count, warning_count=pack.warning_count,
                prepared_by_kind=pack.prepared_by_kind, owner_id=owner, owner_name=user.name if user else None,
                created_at=pack.created_at, updated_at=pack.updated_at,
            )
        )  # fmt: skip
    return PackPage(items=items, total=total, page=page, page_size=page_size)


# ================================================================ writes
async def _write_up(
    pack: SubmissionPack, db: AsyncSession, text: str, source: Literal["agent", "user"]
) -> None:
    from app.services.submission_packs.rules import effective_rules

    rules = await effective_rules(db, pack.client_id)
    cleaned, _flags = writeup.clean(text, max_words=rules.write_up_max_words)
    pack.summary_text = cleaned
    pack.write_up_status = "drafted" if source == "agent" else "edited"


def _require_open(pack: SubmissionPack) -> None:
    if pack.lifecycle != "open":
        raise ConflictError(
            "This submission pack is no longer open",
            code="pack_not_open",
            details={"lifecycle": pack.lifecycle},
        )


async def save_write_up(
    ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID, text: str, *, source: Literal["agent", "user"]
) -> PackDetailOut:
    pack = await _pack_for(ctx, db, pack_id, DRAFT if source == "agent" else SUBMIT)
    _require_open(pack)
    before = pack.status
    await _write_up(pack, db, text, source)
    pack.lock_version = int(pack.lock_version) + 1
    _restatus(pack)
    await db.flush()
    if pack.status != before:
        _emit_ready(db, pack)
    return await detail(ctx, db, pack)


def _restatus(pack: SubmissionPack) -> None:
    checks = [PackCheckDetail.model_validate(c) for c in pack.checks_detail or []]
    pack.status = _status(checks, pack.summary_text)
    pack.missing = list(missing_of(checks, has_write_up=bool((pack.summary_text or "").strip())))


async def save_write_up_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, text: str
) -> PackDetailOut:
    """Agent path: the write-up of the application's open pack (prepare it first)."""
    await application_for(ctx, db, application_id, DRAFT)
    pack = await open_pack(db, application_id)
    if pack is None:
        raise ValidationFailed("Prepare the pack first (submissions__prepare_pack)", code="pack_not_prepared")
    return await save_write_up(ctx, db, pack.id, text, source="agent")


async def set_rates(
    ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID, rates: PackRatesIn
) -> PackDetailOut:
    pack = await _pack_for(ctx, db, pack_id, SUBMIT)
    p = principal(ctx)
    if p is not None:
        check(p, "compensation:view")
    _require_open(pack)
    await _set_rates(ctx, db, pack, rates)
    return await detail(ctx, db, pack)


async def _set_rates(ctx: RequestContext, db: AsyncSession, pack: SubmissionPack, rates: PackRatesIn) -> None:
    pack.pay_rate, pack.bill_rate = rates.pay_rate, rates.bill_rate
    pack.pay_rate_unit, pack.bill_rate_unit = rates.pay_rate_unit, rates.bill_rate_unit
    pack.currency = (rates.currency or pack.currency or "").upper() or None
    pack.lock_version = int(pack.lock_version) + 1
    await recheck(ctx, db, pack)


async def patch(
    ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID, data: PackPatchIn
) -> PackDetailOut:
    pack = await _pack_for(ctx, db, pack_id, SUBMIT)
    _require_open(pack)
    if data.lock_version != pack.lock_version:
        raise ConflictError(
            "The pack changed since you opened it; reload and try again", code="stale_version",
            details={"lock_version": pack.lock_version},
        )  # fmt: skip
    before = pack.status
    fields = data.model_fields_set
    if "summary_text" in fields and data.summary_text is not None:
        await _write_up(pack, db, data.summary_text, "user")
    if "email_subject" in fields:
        pack.email_subject = (data.email_subject or "").strip() or None
    if data.rates is not None:
        p = principal(ctx)
        if p is not None:
            check(p, "compensation:view")
        await _set_rates(ctx, db, pack, data.rates)
    else:
        pack.lock_version = int(pack.lock_version) + 1
    _restatus(pack)
    await db.flush()
    if pack.status != before:
        _emit_ready(db, pack)
    return await detail(ctx, db, pack)


async def mark_opened(ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID) -> PackDetailOut:
    pack = await _pack_for(ctx, db, pack_id, SUBMIT)
    _require_open(pack)
    pack.opened_in_dialog_at = now()
    await db.flush()
    audit.record("submission_pack.opened", "submission_pack", pack.id, session=db)
    return await detail(ctx, db, pack)


async def discard(
    ctx: RequestContext, db: AsyncSession, pack_id: uuid.UUID, reason: str = "user_discarded"
) -> PackDetailOut:
    pack = await _pack_for(ctx, db, pack_id, SUBMIT)
    _require_open(pack)
    _discard(pack, db, reason)
    await db.flush()
    return await detail(ctx, db, pack)


def _discard(pack: SubmissionPack, db: AsyncSession, reason: str) -> None:
    pack.lifecycle = "discarded"
    pack.discarded_reason = reason[:40]
    pack.lock_version = int(pack.lock_version) + 1
    audit.record("submission_pack.discarded", "submission_pack", pack.id, session=db, reason=reason[:40])


async def discard_for_application(db: AsyncSession, application_id: uuid.UUID, reason: str) -> bool:
    pack = await open_pack(db, application_id, for_update=True)
    if pack is None:
        return False
    _discard(pack, db, reason)
    await db.flush()
    return True


async def link_submission(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    submission_id: uuid.UUID,
) -> SubmissionPack | None:
    """A person created ``submission_id``: the application's open pack becomes ``used`` with the diff of their changes."""
    sub = await db.get(Submission, submission_id)
    if sub is None:
        return None
    pack = await open_pack(db, sub.application_id, for_update=True)
    if pack is None:
        return None
    d = pack_diff(pack, sub)
    pack.lifecycle, pack.submission_id, pack.used_at = "used", sub.id, now()
    pack.user_changes = d.to_json()
    pack.lock_version = int(pack.lock_version) + 1
    await db.flush()
    audit.record(
        "submission_pack.used", "submission_pack", pack.id, session=db, submission_id=str(sub.id),
        changed_fields=list(d.changed_fields),
    )  # fmt: skip
    emit(
        db,
        SubmissionPackUsed(
            tenant_id=pack.tenant_id, pack_id=pack.id, submission_id=sub.id, application_id=pack.application_id,
            changed_fields=d.changed_fields, note_change=d.note_change,
        ),
    )  # fmt: skip
    return pack


# ================================================================ consent request (templated, P1 Secure Link)
async def request_consent(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> tuple[str, str | None]:
    from app.services import privacy

    app = await application_for(ctx, db, application_id, READ)
    demand = await db.get(Demand, app.demand_id)
    assert demand is not None
    if await privacy.has_active_consent(db, app.candidate_id, CONSENT_PURPOSE, demand.client_id):
        return "skipped", "already_granted"
    pack = await open_pack(db, app.id, for_update=True)
    today = now().date()
    if (
        pack is not None
        and pack.consent_request_sent_at is not None
        and pack.consent_request_sent_at.date() == today
    ):
        return "skipped", "already_requested_today"
    result = await privacy.request_consents(ctx, db, [app.candidate_id], CONSENT_PURPOSE, demand.client_id)
    status = result.items[0].status if result.items else "not_found"
    if status != "sent":
        return "skipped", str(status)
    if pack is not None:
        pack.consent_request_sent_at = now()
        await db.flush()
    return "sent", None


# ================================================================ agent context (read tool)
async def pack_context(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> PackContextOut:
    from app.models.interviews import Interview, InterviewStage, Scorecard
    from app.models.matching import Match
    from app.models.pipeline import Screening
    from app.services import pipeline_service

    app = await application_for(ctx, db, application_id, PACK_READ)
    facts = await load_facts(db, app)
    cand, demand, rules = facts.candidate, facts.demand, facts.rules
    comp = can_comp(ctx)
    skills = (
        await db.execute(
            select(CandidateSkill.verification, Skill.name, TenantSkill.name)
            .outerjoin(Skill, Skill.id == CandidateSkill.skill_id)
            .outerjoin(TenantSkill, TenantSkill.id == CandidateSkill.tenant_skill_id)
            .where(CandidateSkill.candidate_id == cand.id)
        )
    ).all()
    declared = sorted({(a or b) for v, a, b in skills if (a or b) and v != "inferred"})
    inferred = sorted({(a or b) for v, a, b in skills if (a or b) and v == "inferred"} - set(declared))
    resumes = (
        await db.execute(
            select(CandidateResume)
            .where(CandidateResume.candidate_id == cand.id)
            .order_by(CandidateResume.version.desc().nullslast(), CandidateResume.created_at.desc())
            .limit(5)
        )
    ).scalars()
    screenings = (
        await db.execute(
            select(Screening)
            .where(Screening.application_id == app.id, Screening.status != "draft")
            .order_by(Screening.created_at)
        )
    ).scalars()
    interviews = (
        await db.execute(
            select(InterviewStage.name, Scorecard.recommendation, Scorecard.summary)
            .join(Interview, Interview.id == Scorecard.interview_id)
            .outerjoin(InterviewStage, InterviewStage.id == Interview.stage_id)
            .where(Interview.application_id == app.id, Scorecard.status == "submitted")
            .order_by(Scorecard.submitted_at)
            .limit(10)
        )
    ).all()
    match = (
        await db.execute(
            select(Match.explanation)
            .where(Match.demand_id == demand.id, Match.candidate_id == cand.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    loc = cand.location or {}
    location = (
        ", ".join(str(loc[k]) for k in ("city", "state", "country") if isinstance(loc, dict) and loc.get(k))
        or None
    )
    pack = await open_pack(db, app.id)
    prior = await pipeline_service.prior_submission(
        db, cand.id, demand.client_id, rules.duplicate_window_days
    )

    def money(v: Decimal | None) -> str | None:
        return None if v is None or not comp else f"{Decimal(v):.2f}"

    def summary_of(s: Any) -> str | None:
        text = (s.ai_summary or {}).get("summary") if isinstance(s.ai_summary, dict) else None
        return (text or s.notes or None) and str(text or s.notes)[:1500]

    return PackContextOut(
        application_id=app.id, stage=facts.stage, shortlisted=facts.stage == "shortlisted",
        candidate=ContextCandidate(
            candidate_id=cand.id, current_title=cand.current_title,
            total_experience_years=round(cand.total_experience_months / 12, 1) if cand.total_experience_months else None,
            location=location, notice_period_days=cand.notice_period_days if rules.show_notice_period else None,
            availability_date=cand.availability_date if rules.show_notice_period else None,
            skills=declared[:40], inferred_skills=inferred[:20], expected_pay=money(cand.expected_ctc_amount),
            expected_pay_unit=cand.expected_ctc_unit if comp else None,
            expected_pay_currency=cand.expected_ctc_currency if comp else None,
        ),
        resumes=[ContextResume(resume_id=r.id, version=r.version, parse_status=r.parse_status, uploaded_at=r.created_at) for r in resumes],
        screening=[ContextScreening(kind=s.kind, outcome=s.outcome, summary=summary_of(s)) for s in screenings],
        interviews=[ContextInterview(stage=n, recommendation=r, feedback_summary=(t or None) and t[:1500]) for n, r, t in interviews],
        match=ContextMatch(explanation=(match or None) and match[:1500]),
        demand=ContextDemand(
            demand_id=demand.id, title=demand.title, client_id=demand.client_id, location=demand.location,
            employment_type=demand.employment_type, bill_rate=money(demand.bill_rate), pay_rate_max=money(demand.pay_rate_max),
            currency=demand.currency, rate_unit=demand.rate_unit, submission_limit=demand.submission_limit,
        ),
        rules=ContextRules(
            resume_format=rules.resume_format, mask_contact_details=rules.mask_contact_details,
            write_up_max_words=rules.write_up_max_words, write_up_instructions=rules.write_up_instructions,
            duplicate_window_days=rules.duplicate_window_days, require_candidate_consent=rules.require_candidate_consent,
            show_notice_period=rules.show_notice_period, consent_mode=rules.consent_mode,
        ),
        prior_submissions_to_client=int(prior is not None),
        open_pack=ContextPack(
            pack_id=pack.id, human_id=pack.human_id, status=pack.status, write_up_status=pack.write_up_status,
            consent_request_sent_at=pack.consent_request_sent_at, blocker_count=pack.blocker_count,
        ) if pack is not None else None,
    )  # fmt: skip


async def application_owner(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Accountable Owner of a submission run: the Application owner, else the Demand owner."""
    if subject is None or subject[0] != "application":
        return None
    app = await db.get(Application, subject[1])
    if app is None:
        return None
    if app.owner_id is not None:
        return app.owner_id
    demand = await db.get(Demand, app.demand_id)  # type: ignore[unreachable]
    return demand.owner_id if demand is not None else None


async def agent_status(ctx: RequestContext, db: AsyncSession) -> dict[str, bool]:  # ruff: ignore[unused-function-argument]
    """``{enabled, paused}``: the Submission Agent's feature switches and the kill switches (for the panel buttons)."""
    from app.services.agent_runtime_service import agentic_state
    from app.services.ai import gateway as ai_gateway

    features = await ai_gateway.features_map(db)
    enabled = bool(features.get("agentic") and features.get("agent_submission"))
    state = await agentic_state(RequestContext(), db)
    paused = bool(state.kill_switch or state.platform_kill_switch or state.global_kill_switch)
    return {"enabled": enabled and not paused, "paused": paused}


# ================================================================ metrics
async def metrics(
    ctx: RequestContext, db: AsyncSession, *, date_from: date | None = None, date_to: date | None = None
) -> PackMetricsOut:
    """Median shortlisted -> submitted (hours) with / without a pack, and the share of packs sent with <= 1 change."""
    from app.models.pipeline import ApplicationStageHistory, PipelineStage

    p = principal(ctx)
    if p is not None and not p.has(PACK_READ, Scope.BU):
        raise forbidden(PACK_READ)
    end = date_to or now().date()
    start = date_from or (end - timedelta(days=90))
    lo = datetime.combine(start, time.min, UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, UTC)
    subs = (
        await db.execute(
            select(Submission.id, Submission.application_id, Submission.created_at).where(
                Submission.created_at >= lo, Submission.created_at < hi
            )
        )
    ).all()
    packs = {
        row.submission_id: row
        for row in (
            await db.execute(
                select(SubmissionPack).where(
                    SubmissionPack.submission_id.in_([s.id for s in subs] or [uuid.uuid4()])
                )
            )
        ).scalars()
    }
    shortlisted_ids = select(PipelineStage.id).where(PipelineStage.key == "shortlisted")
    entered = dict(
        (
            await db.execute(
                select(ApplicationStageHistory.application_id, func.max(ApplicationStageHistory.at))
                .where(
                    ApplicationStageHistory.to_stage_id.in_(shortlisted_ids),
                    ApplicationStageHistory.application_id.in_(
                        [s.application_id for s in subs] or [uuid.uuid4()]
                    ),
                )
                .group_by(ApplicationStageHistory.application_id)
            )
        ).tuples().all()
    )
    with_pack: list[float] = []
    without: list[float] = []
    for s in subs:
        at = entered.get(s.application_id)
        if at is None or s.created_at < at:
            continue
        hours = (s.created_at - at).total_seconds() / 3600
        (with_pack if s.id in packs else without).append(hours)
    used = list(packs.values())
    low = sum(1 for pk in used if len((pk.user_changes or {}).get("changed_fields") or []) <= 1)
    open_n = int(
        (
            await db.execute(
                select(func.count()).select_from(SubmissionPack).where(SubmissionPack.lifecycle == "open")
            )
        ).scalar_one()
    )
    blocked_n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(SubmissionPack)
                .where(SubmissionPack.lifecycle == "open", SubmissionPack.status == "blocked")
            )
        ).scalar_one()
    )
    return PackMetricsOut(
        period_from=start, period_to=end, submissions_with_pack=len(with_pack), submissions_without_pack=len(without),
        median_hours_with_pack=round(statistics.median(with_pack), 1) if with_pack else None,
        median_hours_without_pack=round(statistics.median(without), 1) if without else None,
        packs_used=len(used), packs_low_change=low, pct_low_change=round(100 * low / len(used), 1) if used else None,
        open_packs=open_n, blocked_packs=blocked_n,
    )  # fmt: skip


__all__ = [
    "agent_status",
    "application_owner",
    "detail",
    "discard",
    "discard_for_application",
    "get_detail",
    "get_open_pack",
    "link_submission",
    "list_packs",
    "mark_opened",
    "metrics",
    "pack_context",
    "patch",
    "prepare",
    "prepare_pack",
    "recheck",
    "recheck_for_candidate",
    "request_consent",
    "run_checks",
    "save_write_up",
    "save_write_up_for_application",
    "set_rates",
    "stub_out",
]
