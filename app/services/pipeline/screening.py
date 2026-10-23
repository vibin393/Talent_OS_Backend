"""Recruiter screening (story 6.2, FR-34) and AI screening (story 6.3, FR-35).

* Questionnaires are tenant-configurable; the render-time form appends skill questions from the demand.
* Outcomes are human-only: ``reject`` moves the application to Rejected (same transaction), ``proceed`` sets
  ``screening_passed`` and may shortlist, ``hold`` changes nothing. AI screenings need a review first.
* Answers mapped to candidate fields are only *proposed*; ``apply_candidate_updates`` writes them after the
  recruiter confirmed (candidates domain port, audited).
* AI screening: ``generate`` (questions from the demand only -- no candidate data), ``send`` (secure link by
  email from a Celery task), the candidate consents and answers on the public page, the summary runs in a
  task (``ai_summary``), a recruiter reviews it and then sets the outcome.
"""

import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.mailer import EmailMessage, send_email
from app.core.permissions import check
from app.events.pipeline import AIScreeningAnswered, AIScreeningSent, ScreeningCompleted
from app.models.pipeline import Application, Screening, ScreeningQuestionnaire
from app.schemas.pipeline import (
    AIQuestion,
    ApplyUpdatesOut,
    OutcomeIn,
    ProposedUpdate,
    Question,
    QuestionnaireCreate,
    QuestionnaireOut,
    QuestionnaireUpdate,
    QuestionSet,
    ScreeningCreate,
    ScreeningFormOut,
    ScreeningOut,
    ScreeningUpdate,
)
from app.services import ai
from app.services.pipeline.common import (
    READ,
    REJECT,
    SCREEN,
    ServiceUnavailable,
    actor_id,
    actor_kind_of,
    application_for,
    conflict,
    now,
    principal_of,
    tenant_of,
    timeline,
)

AI_FEATURE = "ai_screening"
QUESTIONS_PROMPT = "screening_questions.v1"
LINK_PURPOSE = "ai_screening"
SUBJECT_TYPE = "screening"
MAX_SKILL_QUESTIONS = 10

DEFAULT_QUESTIONNAIRE_NAME = "Default screening"
DEFAULT_QUESTIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "interest",
        "label": "Is the candidate interested in this role?",
        "type": "boolean",
        "required": True,
    },
    {
        "key": "availability",
        "label": "Earliest date the candidate can start",
        "type": "date",
        "required": False,
    },
    {
        "key": "expected_compensation",
        "label": "Expected compensation",
        "type": "money",
        "required": False,
        "maps_to": "expected_ctc",
    },
    {
        "key": "notice_period",
        "label": "Notice period (days)",
        "type": "number",
        "required": False,
        "maps_to": "notice_period_days",
    },
    {
        "key": "work_authorization",
        "label": "Work authorization",
        "type": "text",
        "required": False,
        "maps_to": "work_authorization",
    },
    {
        "key": "relocation",
        "label": "Willing to relocate?",
        "type": "boolean",
        "required": False,
        "maps_to": "willing_to_relocate",
    },
    {"key": "communication", "label": "Communication (1-5)", "type": "rating", "required": False},
)


# ================================================================ questionnaires
async def ensure_default_questionnaire(db: AsyncSession) -> ScreeningQuestionnaire:
    row = (
        await db.execute(
            select(ScreeningQuestionnaire).where(
                ScreeningQuestionnaire.is_default.is_(True), ScreeningQuestionnaire.archived_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = ScreeningQuestionnaire(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=DEFAULT_QUESTIONNAIRE_NAME,
        questions=[dict(q) for q in DEFAULT_QUESTIONS],
        is_default=True,
    )
    db.add(row)
    await db.flush()
    return row


async def list_questionnaires(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    include_archived: bool = False,
) -> list[QuestionnaireOut]:
    await ensure_default_questionnaire(db)
    stmt = select(ScreeningQuestionnaire).order_by(
        ScreeningQuestionnaire.is_default.desc(), ScreeningQuestionnaire.name
    )
    if not include_archived:
        stmt = stmt.where(ScreeningQuestionnaire.archived_at.is_(None))
    return [QuestionnaireOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]


def _unique_keys(questions: list[Question]) -> None:
    keys = [q.key for q in questions]
    if len(keys) != len(set(keys)):
        raise ValidationFailed("Question keys must be unique", code="duplicate_question_key")
    for q in questions:
        if q.type == "choice" and not q.options:
            raise ValidationFailed(
                "Choice questions need options", code="invalid_question", details={"key": q.key}
            )


async def _clear_default(db: AsyncSession, keep: uuid.UUID) -> None:
    await db.execute(
        update(ScreeningQuestionnaire)
        .where(ScreeningQuestionnaire.id != keep, ScreeningQuestionnaire.is_default.is_(True))
        .values(is_default=False)
    )


async def create_questionnaire(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    data: QuestionnaireCreate,
) -> QuestionnaireOut:
    _unique_keys(data.questions)
    row = ScreeningQuestionnaire(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name,
        questions=[q.model_dump(exclude_none=True) for q in data.questions],
        is_default=False,
    )
    db.add(row)
    await db.flush()
    if data.is_default:
        await _clear_default(db, row.id)
        await db.flush()
        row.is_default = True
        await db.flush()
    await db.refresh(row)
    return QuestionnaireOut.model_validate(row)


async def _questionnaire(db: AsyncSession, questionnaire_id: uuid.UUID) -> ScreeningQuestionnaire:
    row = await db.get(ScreeningQuestionnaire, questionnaire_id)
    if row is None:
        raise NotFound("Questionnaire not found")
    return row


async def get_questionnaire(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    questionnaire_id: uuid.UUID,
) -> QuestionnaireOut:
    return QuestionnaireOut.model_validate(await _questionnaire(db, questionnaire_id))


async def update_questionnaire(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    questionnaire_id: uuid.UUID,
    data: QuestionnaireUpdate,
) -> QuestionnaireOut:
    row = await _questionnaire(db, questionnaire_id)
    if data.name is not None:
        row.name = data.name
    if data.questions is not None:
        _unique_keys(data.questions)
        row.questions = [q.model_dump(exclude_none=True) for q in data.questions]
    if data.is_default is True and not row.is_default:
        if row.archived_at is not None:
            raise ConflictError(
                "An archived questionnaire cannot be the default", code="questionnaire_archived"
            )
        await _clear_default(db, row.id)
        await db.flush()
        row.is_default = True
    elif data.is_default is False:
        row.is_default = False
    await db.flush()
    await db.refresh(row)
    return QuestionnaireOut.model_validate(row)


async def archive_questionnaire(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    questionnaire_id: uuid.UUID,
) -> QuestionnaireOut:
    row = await _questionnaire(db, questionnaire_id)
    if row.archived_at is None:
        row.archived_at = now()
        row.is_default = False
        await db.flush()
    await db.refresh(row)
    return QuestionnaireOut.model_validate(row)


# ================================================================ form
def _skill_key(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "skill"
    return f"skill_{slug}_years"


async def skill_questions(db: AsyncSession, demand_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.services.demands import pipeline_port as demands_port

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for skill in await demands_port.skills(db, demand_id):
        key = _skill_key(skill["label"])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "key": key,
                "label": f"Years of hands-on experience with {skill['label']}?",
                "type": "number",
                "required": skill["kind"] == "mandatory",
            }
        )
        if len(out) >= MAX_SKILL_QUESTIONS:
            break
    return out


async def _form_questions(
    db: AsyncSession, app: Application, questionnaire_id: uuid.UUID | None
) -> tuple[uuid.UUID, list[dict[str, Any]]]:
    if questionnaire_id is not None:
        qn = await _questionnaire(db, questionnaire_id)
        if qn.archived_at is not None:
            raise ConflictError("The questionnaire is archived", code="questionnaire_archived")
    else:
        qn = await ensure_default_questionnaire(db)
    questions = [dict(q) for q in qn.questions]
    keys = {q["key"] for q in questions}
    questions += [q for q in await skill_questions(db, app.demand_id) if q["key"] not in keys]
    return qn.id, questions


async def screening_form(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    questionnaire_id: uuid.UUID | None = None,
) -> ScreeningFormOut:
    app = await application_for(ctx, db, application_id, READ)
    qid, questions = await _form_questions(db, app, questionnaire_id)
    return ScreeningFormOut(questionnaire_id=qid, questions=[Question.model_validate(q) for q in questions])


# ================================================================ answers
def _bad(key: str, message: str) -> ValidationFailed:
    return ValidationFailed("Invalid answers", code="invalid_answers", details={"fields": {key: message}})


def normalize_answers(
    questions: list[dict[str, Any]], answers: dict[str, Any], *, require: bool
) -> dict[str, Any]:
    """Validate ``answers`` against the question snapshot (unknown keys: 422 ``invalid_answers``)."""
    by_key = {q["key"]: q for q in questions}
    out: dict[str, Any] = {}
    for key, value in answers.items():
        q = by_key.get(key)
        if q is None:
            raise _bad(key, "Unknown question")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        kind = q.get("type")
        if kind == "text":
            if not isinstance(value, str | int | float):
                raise _bad(key, "Expected text")
            out[key] = str(value).strip()[:4000]
        elif kind in ("number", "rating"):
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise _bad(key, "Expected a number") from exc
            if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
                raise _bad(key, "Out of range")
            if kind == "rating" and not (1 <= number <= 5):
                raise _bad(key, "Rating must be between 1 and 5")
            out[key] = int(number) if number == number.to_integral_value() else float(number)
        elif kind == "boolean":
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, str) and value.lower() in ("yes", "true", "no", "false"):
                out[key] = value.lower() in ("yes", "true")
            else:
                raise _bad(key, "Expected yes or no")
        elif kind == "choice":
            if value not in (q.get("options") or []):
                raise _bad(key, "Not one of the options")
            out[key] = value
        elif kind == "money":
            amount, currency = (
                (value.get("amount"), value.get("currency")) if isinstance(value, dict) else (value, None)
            )
            try:
                number = Decimal(str(amount))
            except (InvalidOperation, ValueError) as exc:
                raise _bad(key, "Expected an amount") from exc
            if not number.is_finite() or number < 0:
                raise _bad(key, "Expected an amount")
            if currency is not None and not (
                isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)
            ):
                raise _bad(key, "Invalid currency")
            out[key] = {"amount": str(number.quantize(Decimal("0.01"))), "currency": currency}
        elif kind == "date":
            try:
                out[key] = date.fromisoformat(str(value)).isoformat()
            except ValueError as exc:
                raise _bad(key, "Expected a date (YYYY-MM-DD)") from exc
        else:
            out[key] = value
    if require:
        missing = [q["key"] for q in questions if q.get("required") and q["key"] not in out]
        if missing:
            raise ValidationFailed(
                "Required questions are unanswered",
                code="answers_required",
                details={"fields": dict.fromkeys(missing, "Required")},
            )
    return out


def _mapped_value(q: dict[str, Any], value: Any) -> Any:
    if q.get("maps_to") == "expected_ctc" and not isinstance(value, dict):
        return {"amount": str(value), "currency": None}
    return value


async def proposals(db: AsyncSession, row: Screening) -> list[ProposedUpdate]:
    from app.services.candidates import pipeline_port as candidates

    mapped = [q for q in row.questions if q.get("maps_to") and q["key"] in (row.answers or {})]
    if not mapped:
        return []
    current = await candidates.screening_fields(db, row.candidate_id)
    out: list[ProposedUpdate] = []
    for q in mapped:
        field = q["maps_to"]
        proposed = _mapped_value(q, row.answers[q["key"]])
        cur = current.get(field)
        same = cur == proposed
        if field == "expected_ctc" and isinstance(cur, dict) and isinstance(proposed, dict):
            same = Decimal(str(cur.get("amount"))) == Decimal(str(proposed.get("amount"))) and (
                proposed.get("currency") in (None, cur.get("currency"))
            )
        if not same:
            out.append(ProposedUpdate(field=field, current=cur, proposed=proposed))
    return out


async def screening_out(db: AsyncSession, row: Screening, *, with_proposals: bool = True) -> ScreeningOut:
    link_expires = None
    if row.secure_link_id is not None:
        link = await db.get(secure_links.SecureLink, row.secure_link_id)
        link_expires = link.expires_at if link is not None else None
    return ScreeningOut(
        id=row.id,
        application_id=row.application_id,
        candidate_id=row.candidate_id,
        kind=row.kind,
        questionnaire_id=row.questionnaire_id,
        questions=list(row.questions or []),
        answers=dict(row.answers or {}),
        notes=row.notes,
        outcome=row.outcome,
        reason_code=row.reason_code,
        status=row.status,
        ai_summary=row.ai_summary,
        ai_generated=row.ai_generated,
        ai_action_id=row.ai_action_id,
        summary_status=row.summary_status,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        secure_link_id=row.secure_link_id,
        link_expires_at=link_expires,
        answered_at=row.answered_at,
        completed_by=row.completed_by,
        completed_at=row.completed_at,
        created_at=row.created_at,
        proposed_candidate_updates=await proposals(db, row) if with_proposals else [],
    )


async def screenings_for(ctx: RequestContext, db: AsyncSession, app: Application) -> list[ScreeningOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(
            select(Screening).where(Screening.application_id == app.id).order_by(Screening.created_at.desc())
        )
    ).scalars()
    return [await screening_out(db, r, with_proposals=False) for r in rows]


async def list_screenings(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[ScreeningOut]:
    app = await application_for(ctx, db, application_id, READ)
    return await screenings_for(ctx, db, app)


async def _screening_for(
    ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID, permission: str
) -> tuple[Screening, Application]:
    row = await db.get(Screening, screening_id, with_for_update=True)
    if row is None:
        raise NotFound("Screening not found")
    try:
        app = await application_for(ctx, db, row.application_id, permission)
    except NotFound as exc:
        raise NotFound("Screening not found") from exc
    return row, app


async def get_screening(ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID) -> ScreeningOut:
    row, _ = await _screening_for(ctx, db, screening_id, READ)
    return await screening_out(db, row)


# ================================================================ recruiter screening
async def create_screening(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, data: ScreeningCreate
) -> ScreeningOut:
    app = await application_for(ctx, db, application_id, SCREEN)
    qid, questions = await _form_questions(db, app, data.questionnaire_id)
    answers = normalize_answers(questions, data.answers, require=data.outcome is not None)
    row = Screening(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        application_id=app.id,
        candidate_id=app.candidate_id,
        kind="recruiter",
        questionnaire_id=qid,
        questions=questions,
        answers=answers,
        notes=data.notes,
        status="draft",
        created_by=actor_id(ctx),
    )
    db.add(row)
    await db.flush()
    if data.outcome is not None:
        await _apply_outcome(
            ctx,
            db,
            row,
            app,
            OutcomeIn(
                outcome=data.outcome,
                reason_code=data.reason_code,
                note=data.note,
                move_to_shortlisted=data.move_to_shortlisted,
                send_rejection_email=data.send_rejection_email,
            ),
        )
    await db.refresh(row)
    return await screening_out(db, row)


async def update_screening(
    ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID, data: ScreeningUpdate
) -> ScreeningOut:
    row, _ = await _screening_for(ctx, db, screening_id, SCREEN)
    if row.status == "completed":
        raise conflict("screening_completed", "A completed screening cannot be edited")
    if data.questions is not None:
        if row.kind != "ai" or row.status != "draft":
            raise conflict("screening_locked", "Questions can only be edited on an unsent AI screening")
        _unique_keys(data.questions)
        row.questions = [q.model_dump(exclude_none=True) for q in data.questions]
    if data.answers is not None:
        if row.kind == "ai":
            raise conflict("screening_locked", "The candidate answers an AI screening")
        row.answers = normalize_answers(row.questions, {**row.answers, **data.answers}, require=False)
    if data.notes is not None:
        row.notes = data.notes
    await db.flush()
    await db.refresh(row)
    return await screening_out(db, row)


async def _apply_outcome(
    ctx: RequestContext, db: AsyncSession, row: Screening, app: Application, data: OutcomeIn
) -> None:
    from app.services.pipeline.applications import transition_application

    actor = actor_kind_of(ctx)
    if actor != "user":
        raise conflict("human_only_action", "Only a person can set a screening outcome")
    if row.status == "completed":
        raise conflict("screening_completed", "The screening already has an outcome")
    if row.kind == "ai" and row.reviewed_at is None:
        raise conflict("review_required", "Review the AI screening before setting its outcome")
    if row.kind == "recruiter":
        row.answers = normalize_answers(row.questions, row.answers or {}, require=True)
    if data.outcome == "reject":
        check(principal_of(ctx), REJECT)
        await transition_application(
            ctx,
            db,
            app.id,
            "rejected",
            "user",
            data.reason_code,
            data.note,
            send_rejection_email=data.send_rejection_email,
        )
    elif data.outcome == "proceed":
        app.screening_passed = True
        await db.flush()
        if data.move_to_shortlisted:
            await transition_application(ctx, db, app.id, "shortlisted", "user", note=data.note)
    row.outcome = data.outcome
    row.reason_code = data.reason_code if data.outcome == "reject" else None
    row.status = "completed"
    row.completed_by = actor_id(ctx)
    row.completed_at = now()
    await db.flush()
    await timeline(
        ctx,
        db,
        "candidate",
        app.candidate_id,
        f"{app.human_id}: screening {data.outcome}",
        {"application_id": str(app.id), "screening_id": str(row.id), "kind": row.kind},
    )
    emit(
        db,
        ScreeningCompleted(
            tenant_id=app.tenant_id,
            screening_id=row.id,
            application_id=app.id,
            kind=row.kind,
            outcome=data.outcome,
        ),
    )


async def set_outcome(
    ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID, data: OutcomeIn
) -> ScreeningOut:
    """Human-only (FR-65). ``reject`` -> Rejected (reason required) in the same transaction."""
    row, app = await _screening_for(ctx, db, screening_id, SCREEN)
    app = (
        await db.execute(select(Application).where(Application.id == app.id).with_for_update())
    ).scalar_one()
    await _apply_outcome(ctx, db, row, app, data)
    await db.refresh(row)
    return await screening_out(db, row)


async def apply_candidate_updates(
    ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID, fields: list[str]
) -> ApplyUpdatesOut:
    """Write the chosen proposed values to the candidate (explicit confirmation, FR-34)."""
    from app.services.candidates import pipeline_port as candidates

    row, _ = await _screening_for(ctx, db, screening_id, SCREEN)
    by_field = {p.field: p.proposed for p in await proposals(db, row)}
    unknown = [f for f in fields if f not in by_field]
    if unknown:
        raise ValidationFailed(
            "No proposed value for these fields",
            code="no_proposal",
            details={"fields": dict.fromkeys(unknown, "No proposed value")},
        )
    applied = await candidates.update_candidate_fields(
        ctx, db, row.candidate_id, {f: by_field[f] for f in fields}  # type: ignore[index]
    )
    audit.record("screening.candidate_updates_applied", "screening", row.id, session=db, fields=applied)
    return ApplyUpdatesOut(applied=applied)


# ================================================================ AI screening
async def generate_ai_screening(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> ScreeningOut:
    """Draft AI screening questions from the demand only (no candidate data). 503 ``ai_unavailable``."""
    from app.services.demands import pipeline_port as demands_port

    app = await application_for(ctx, db, application_id, SCREEN)
    demand = await demands_port.get_row(db, app.demand_id)
    skills = await demands_port.skills(db, demand.id)
    role = {
        "title": demand.title,
        "jd_text": ((await demands_port.jd_text(db, demand)) or "")[:12000],
        "required_skills": [s["label"] for s in skills],
        "must_haves": [
            f"{s['label']}" + (f" ({s['min_years']}+ years)" if s.get("min_years") else "")
            for s in skills
            if s["kind"] == "mandatory"
        ],
        "min_experience_years": float(demand.min_experience_years) if demand.min_experience_years else None,
    }
    result = await ai.run(
        ctx,
        db,
        AI_FEATURE,
        QUESTIONS_PROMPT,
        inputs={},
        untrusted={"role": json.dumps(role, ensure_ascii=False, indent=1)},
        output_schema=QuestionSet,
        subject=None,
        input_ref=f"demand:{demand.id}",
    )
    if not result.ok or not isinstance(result.data, QuestionSet):
        raise ServiceUnavailable(
            "AI question generation is unavailable; use the default questionnaire",
            code="ai_unavailable",
            details={"reason": result.error_code},
        )
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in result.data.questions:
        item: AIQuestion = q
        if item.key in seen or (item.type == "choice" and not item.options):
            continue
        seen.add(item.key)
        questions.append(item.model_dump(exclude_none=True))
    row = Screening(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        application_id=app.id,
        candidate_id=app.candidate_id,
        kind="ai",
        questions=questions,
        answers={},
        status="draft",
        questions_ai_action_id=result.action_id,
        created_by=actor_id(ctx),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return await screening_out(db, row)


async def send_ai_screening(ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID) -> ScreeningOut:
    """Request the invitation (link + email go out from a Celery task after commit). Resending revokes the
    previous link. 409 ``candidate_email_missing``."""
    from app.services.candidates import pipeline_port as candidates

    row, app = await _screening_for(ctx, db, screening_id, SCREEN)
    if row.kind != "ai":
        raise conflict("not_ai_screening", "Only AI screenings are sent to candidates")
    if row.status not in ("draft", "sent", "expired"):
        raise conflict("screening_not_sendable", "This screening can no longer be sent", status=row.status)
    if not row.questions:
        raise ValidationFailed("The screening has no questions", code="questions_required")
    if await candidates.primary_email(db, row.candidate_id) is None:
        raise conflict("candidate_email_missing", "The candidate has no email address")
    await secure_links.revoke_for_subject(db, SUBJECT_TYPE, row.id, LINK_PURPOSE)
    row.status = "sent"
    row.secure_link_id = None
    row.invite_requested_at = now()
    await db.flush()
    await timeline(
        ctx,
        db,
        "candidate",
        app.candidate_id,
        f"{app.human_id}: AI screening sent",
        {"application_id": str(app.id), "screening_id": str(row.id)},
    )
    emit(db, AIScreeningSent(tenant_id=app.tenant_id, screening_id=row.id, application_id=app.id))
    await db.refresh(row)
    return await screening_out(db, row)


async def deliver_invite(ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID) -> bool:
    """Task body: issue the secure link and email the candidate (idempotent per send request)."""
    from app.services import identity_bridge, settings_service
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    row = await db.get(Screening, screening_id, with_for_update=True)
    if row is None or row.kind != "ai" or row.status != "sent" or row.invite_requested_at is None:
        return False
    if row.invite_sent_at is not None and row.invite_sent_at >= row.invite_requested_at:
        return False
    email = await candidates.primary_email(db, row.candidate_id)
    if email is None:
        return False
    app = await db.get(Application, row.application_id)
    assert app is not None
    await secure_links.revoke_for_subject(db, SUBJECT_TYPE, row.id, LINK_PURPOSE)
    ttl = await settings_service.link_ttl(db, LINK_PURPOSE)
    link, token = await secure_links.issue(
        db,
        tenant_id=row.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type=SUBJECT_TYPE,
        subject_id=row.id,
        ttl=ttl,
        created_by=row.created_by,
    )
    row.secure_link_id = link.id
    row.invite_sent_at = now()
    tenant = await identity_bridge.get_tenant(db, row.tenant_id)
    demand = await demands_port.get_row(db, app.demand_id)
    tenant_name = tenant.name if tenant is not None else "Our team"
    await db.flush()
    await send_email(
        EmailMessage(
            to=email,
            subject=f"{tenant_name}: a few questions about the {demand.title} role",
            text=(
                f"Hello,\n\n{tenant_name} would like to learn a little more about you for the {demand.title} "
                f"role. Please answer a few short questions here:\n{secure_links.link_url(token)}\n\n"
                f"The link expires in {ttl.days} days and can be used once.\n"
            ),
            tenant_id=row.tenant_id,
            kind="ai_screening_invite",
        )
    )
    del ctx
    return True


async def review(ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID) -> ScreeningOut:
    """Mark an answered AI screening as reviewed by a person (required before an outcome)."""
    row, _ = await _screening_for(ctx, db, screening_id, SCREEN)
    if actor_kind_of(ctx) != "user":
        raise conflict("human_only_action", "Only a person can review an AI screening")
    if row.kind != "ai":
        raise conflict("not_ai_screening", "Only AI screenings need a review")
    if row.status not in ("answered", "reviewed"):
        raise conflict("screening_not_answered", "The candidate has not answered yet", status=row.status)
    if row.reviewed_at is None:
        row.reviewed_by = actor_id(ctx)
        row.reviewed_at = now()
        row.status = "reviewed"
        await db.flush()
    await db.refresh(row)
    return await screening_out(db, row)


async def request_summary(ctx: RequestContext, db: AsyncSession, screening_id: uuid.UUID) -> ScreeningOut:
    """Retry the AI summary (after a failure): enqueued after commit."""
    from app.events.pipeline import AIScreeningSummaryRequested

    row, app = await _screening_for(ctx, db, screening_id, SCREEN)
    if row.kind != "ai" or row.status not in ("answered", "reviewed"):
        raise conflict("screening_not_answered", "There are no answers to summarise", status=row.status)
    row.summary_status = "pending"
    await db.flush()
    emit(db, AIScreeningSummaryRequested(tenant_id=app.tenant_id, screening_id=row.id))
    await db.refresh(row)
    return await screening_out(db, row)


# ================================================================ public (candidate) side
async def public_consent(
    db: AsyncSession,
    row: Screening,
    link_id: uuid.UUID,
    granted: bool,
    notice_id: uuid.UUID | None,
    evidence: dict[str, Any],
) -> str:
    """AI-processing consent on the screening page. Decline consumes the link and notifies the owner."""
    from app.services import privacy

    ctx = RequestContext(tenant_id=row.tenant_id)
    app = await db.get(Application, row.application_id)
    assert app is not None
    if row.status != "sent":
        raise conflict("screening_not_open", "This screening is no longer open")
    if granted:
        await privacy.grant_consent(
            ctx,
            db,
            row.candidate_id,
            "ai_processing",
            source="candidate_link",
            notice_id=notice_id,
            evidence={**evidence, "link_id": str(link_id)},
        )
        return "granted"
    await secure_links.consume(db, link_id)
    row.status = "consent_declined"
    await db.flush()
    audit.record("consent.declined", "candidate", row.candidate_id, session=db, purpose="ai_processing")
    await timeline(
        ctx,
        db,
        "candidate",
        app.candidate_id,
        f"{app.human_id}: candidate declined AI screening consent",
        {"application_id": str(app.id), "screening_id": str(row.id)},
    )
    await timeline(
        ctx,
        db,
        "demand",
        app.demand_id,
        f"{app.human_id}: candidate declined AI screening consent",
        {"application_id": str(app.id), "screening_id": str(row.id)},
    )
    return "declined"


async def public_submit(db: AsyncSession, row: Screening, link_id: uuid.UUID, answers: dict[str, Any]) -> str:
    from app.services import privacy

    if row.status != "sent":
        raise conflict("screening_not_open", "This screening is no longer open")
    if not await privacy.has_active_consent(db, row.candidate_id, "ai_processing"):
        raise conflict("consent_required", "Please give consent before answering")
    clean = normalize_answers(row.questions, answers, require=True)
    await secure_links.consume(db, link_id)
    app = await db.get(Application, row.application_id)
    assert app is not None
    row.answers = clean
    row.status = "answered"
    row.answered_at = now()
    row.summary_status = "pending"
    await db.flush()
    ctx = RequestContext(tenant_id=row.tenant_id)
    await timeline(
        ctx,
        db,
        "candidate",
        app.candidate_id,
        f"{app.human_id}: AI screening answered",
        {"application_id": str(app.id), "screening_id": str(row.id)},
    )
    emit(db, AIScreeningAnswered(tenant_id=row.tenant_id, screening_id=row.id, application_id=app.id))
    return "answered"


# ======================================================== Epic 57 (additive functions, coordinator C-11)
def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _tolerant_answers(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, Any]:
    """Normalise each answer on its own; invalid values are dropped (the recruiter reviews them)."""
    keys = {q["key"] for q in questions}
    out: dict[str, Any] = {}
    for key, value in answers.items():
        if key not in keys:
            continue
        try:
            out.update(normalize_answers(questions, {key: value}, require=False))
        except ValidationFailed:
            continue
    return out


async def record_agent_result(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    questions_snapshot: list[dict[str, Any]],
    answers: dict[str, Any],
    summary: Any,
    source_ref: str,
) -> uuid.UUID:
    """Epic 57: a completed Screening Agent conversation becomes an answered AI screening (``kind='ai'``,
    ``status='answered'``, ``ai_generated``). The P1 review and the human-only outcome then apply unchanged.
    ``source_ref``: the id of an AI screening an earlier call created (re-used), else a new row.
    Never moves a stage or sets an outcome."""
    app = await db.get(Application, application_id)
    if app is None:
        raise NotFound("Application not found")
    existing_id = _uuid_or_none(source_ref)
    existing = await db.get(Screening, existing_id) if existing_id is not None else None
    if existing is not None and (existing.application_id != app.id or existing.kind != "ai"):
        existing = None
    payload = summary.model_dump(mode="json") if hasattr(summary, "model_dump") else dict(summary or {})
    row = existing or Screening(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        application_id=app.id,
        candidate_id=app.candidate_id,
        kind="ai",
        created_by=None,
    )
    if row.status in ("reviewed", "completed"):
        return row.id
    row.questions = list(questions_snapshot)
    row.answers = _tolerant_answers(list(questions_snapshot), answers)
    row.status = "answered"
    row.answered_at = now()
    row.ai_summary = payload
    row.ai_generated = True
    row.summary_status = "ready"
    if existing is None:
        db.add(row)
    await db.flush()
    await timeline(
        ctx,
        db,
        "candidate",
        app.candidate_id,
        f"{app.human_id}: AI screening conversation completed",
        {"application_id": str(app.id), "screening_id": str(row.id), "source": "screening_agent"},
    )
    return row.id


async def apply_agent_review(
    ctx: RequestContext,
    db: AsyncSession,
    screening_id: uuid.UUID,
    answers: dict[str, Any],
    reviewer: uuid.UUID | None,
) -> ScreeningOut:
    """Epic 57: write the reviewed (accepted / edited) agent answers into the AI screening and mark it
    reviewed through the existing human-only ``review``. ``reviewer`` must be the calling person."""
    if reviewer is None or reviewer != actor_id(ctx):
        raise conflict("human_only_action", "Only the reviewing person can complete the review")
    row, _ = await _screening_for(ctx, db, screening_id, SCREEN)
    if row.kind != "ai":
        raise conflict("not_ai_screening", "Only AI screenings need a review")
    if row.status not in ("answered", "reviewed"):
        raise conflict("screening_not_answered", "The candidate has not answered yet", status=row.status)
    row.answers = _tolerant_answers(list(row.questions or []), answers)
    await db.flush()
    return await review(ctx, db, screening_id)
