# ruff: file-ignore[line-too-long, non-imperative-mood, unused-function-argument]
"""Questionnaire versions and bindings (Epic 57 story 57.1, FR-289; spec A2).

* A version extends a P1 ``screening_questionnaires`` template with typed Agent Questions (stable ids across versions:
  ``uuid5(questionnaire, key)`` unless the editor keeps an explicit id), approved clarification templates and an
  approval. Only the ``approved`` version of a questionnaire is used by the agent; approving retires the previous one.
* AI-generated questions (``origin=ai_generated``) need ``accepted_by`` before approval (422 ``question_not_accepted``).
* Knockout criteria are validated per answer type (422 ``knockout_invalid``) and only ever flag answers.
* ``resolve_for_application``: the most specific approved binding (demand -> client -> tenant); without one, a
  demand-bound questionnaire is created from the P1 defaults plus the demand's skill questions and auto-approved
  (deterministic templates; audit ``screening_questionnaire.default_approved``).
"""

import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.demands import Demand
from app.models.pipeline import Application, ScreeningQuestionnaire
from app.models.screening_agent import ScreeningQuestionnaireBinding, ScreeningQuestionnaireVersion
from app.schemas.screening_agent import (
    MAX_QUESTIONS,
    AgentQuestion,
    BindingIn,
    BindingOut,
    GeneratedQuestionsOut,
    QuestionDraft,
    VersionCreate,
    VersionOut,
    VersionUpdate,
)
from app.services.screening_agent.common import conflict, invalid, now, tenant_of, user_id_of

_NS = uuid.UUID("6d1f8a57-5c9e-4b57-9a57-0e57a57e5700")
NUMERIC_OPS = frozenset({"lt", "lte", "gt", "gte", "eq", "neq"})
CHOICE_OPS = frozenset({"eq", "neq", "in", "not_in"})
BOOLEAN_OPS = frozenset({"eq", "neq"})
DEFAULT_CLARIFICATIONS: dict[str, dict[str, str]] = {
    "number": {
        "ask_number_days": "Could you give that as a number of days? For example: 30.",
        "ask_number": "Could you give that as a number?",
    },
    "date": {"ask_date": "Could you give an exact date? For example: 2026-11-01."},
    "money": {"ask_amount": "Could you give an amount and currency? For example: 12 LPA or INR 1200000."},
    "boolean": {"ask_yes_no": "Could you answer yes or no?"},
    "choice": {"ask_choice": "Could you pick one of the options above?"},
    "text": {"ask_more": "Could you tell me a little more?"},
    "rating": {"ask_rating": "Could you give a number from 1 to 5?"},
}
AUTO_NAME = "AI screening"


def stable_id(questionnaire_id: uuid.UUID, key: str) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{questionnaire_id}:{key}")


def from_p1(
    questionnaire_id: uuid.UUID, questions: list[dict[str, Any]], origin: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for q in questions:
        item = {
            "id": str(stable_id(questionnaire_id, q["key"])),
            "key": q["key"],
            "label": q["label"],
            "type": q["type"],
            "required": bool(q.get("required")),
            "origin": q.get("origin") or origin,
        }
        if q.get("options"):
            item["options"] = list(q["options"])
        if q.get("maps_to"):
            item["maps_to"] = q["maps_to"]
        if q["type"] == "number" and (q["key"] == "notice_period" or "days" in q["label"].lower()):
            item["unit"] = "days"
        if q["type"] == "number" and q["key"].endswith("_years"):
            item["unit"] = "years"
        out.append(AgentQuestion.model_validate(item).model_dump(mode="json", exclude_none=True))
    return out[:MAX_QUESTIONS]


def _knockout_error(key: str, message: str) -> Exception:
    return invalid("knockout_invalid", message, fields={key: message})


def _check_knockout(q: QuestionDraft) -> None:
    rule = q.knockout
    if rule is None:
        return
    if q.type == "text":
        raise _knockout_error(q.key, "Free-text answers cannot have a knockout")
    if q.type in ("number", "rating", "money"):
        if rule.op not in NUMERIC_OPS or isinstance(rule.value, bool):
            raise _knockout_error(q.key, "Use a comparison with a number")
        try:
            float(rule.value)
        except (TypeError, ValueError) as exc:
            raise _knockout_error(q.key, "The knockout value must be a number") from exc
    elif q.type == "date":
        if rule.op not in NUMERIC_OPS:
            raise _knockout_error(q.key, "Use a date comparison")
        try:
            date.fromisoformat(str(rule.value))
        except ValueError as exc:
            raise _knockout_error(q.key, "The knockout value must be a date (YYYY-MM-DD)") from exc
    elif q.type == "boolean":
        if rule.op not in BOOLEAN_OPS or not isinstance(rule.value, bool):
            raise _knockout_error(q.key, "Use 'is' / 'is not' with yes or no")
    elif q.type == "choice":
        if rule.op not in CHOICE_OPS:
            raise _knockout_error(q.key, "Use 'is', 'is not', 'is one of' or 'is not one of'")
        values = rule.value if isinstance(rule.value, list) else [rule.value]
        if not values or any(v not in (q.options or []) for v in values):
            raise _knockout_error(q.key, "The knockout value must be one of the options")


def validate_questions(questionnaire_id: uuid.UUID, drafts: list[QuestionDraft]) -> list[dict[str, Any]]:
    if len(drafts) > MAX_QUESTIONS:
        raise invalid("too_many_questions", f"At most {MAX_QUESTIONS} questions")
    keys: set[str] = set()
    ids: set[uuid.UUID] = set()
    out: list[dict[str, Any]] = []
    for q in drafts:
        if q.key in keys:
            raise invalid(
                "duplicate_question_key", "Question keys must be unique", fields={q.key: "Duplicate key"}
            )
        keys.add(q.key)
        if q.type == "choice" and not q.options:
            raise invalid("options_required", "Choice questions need options", fields={q.key: "Add options"})
        if q.min is not None and q.max is not None and q.min > q.max:
            raise invalid("invalid_range", "Minimum is above maximum", fields={q.key: "Check min / max"})
        _check_knockout(q)
        qid = q.id or stable_id(questionnaire_id, q.key)
        if qid in ids:
            raise invalid("duplicate_question_id", "Question ids must be unique")
        ids.add(qid)
        item = q.model_dump(mode="json", exclude_none=True)
        item["id"] = str(qid)
        if item.get("origin") != "ai_generated":
            item.pop("accepted_by", None)
        out.append(AgentQuestion.model_validate(item).model_dump(mode="json", exclude_none=True))
    return out


def knockout_hit(question: dict[str, Any], value: Any) -> bool:
    """Whether a normalised value meets the knockout criterion (display flag only -- never a rejection)."""
    rule = question.get("knockout")
    if not rule or value is None:
        return False
    op, target = rule.get("op"), rule.get("value")
    kind = question.get("type")
    left: Any
    right: Any
    try:
        if kind in ("number", "rating"):
            left, right = float(value), float(target)
        elif kind == "money":
            left = float(value.get("amount") if isinstance(value, dict) else value)  # type: ignore[arg-type]
            right = float(target)
        elif kind == "date":
            left, right = date.fromisoformat(str(value)), date.fromisoformat(str(target))
        elif kind == "boolean":
            left, right = bool(value), bool(target)
        elif kind == "choice":
            values = target if isinstance(target, list) else [target]
            if op in ("in", "eq"):
                return value in values
            if op in ("not_in", "neq"):
                return value not in values
            return False
        else:
            return False
    except (TypeError, ValueError, AttributeError):
        return False
    return {  # type: ignore[no-any-return]
        "lt": left < right, "lte": left <= right, "gt": left > right, "gte": left >= right,
        "eq": left == right, "neq": left != right,
    }.get(str(op), False)  # fmt: skip


# ================================================================ reads
async def _questionnaire(db: AsyncSession, questionnaire_id: uuid.UUID) -> ScreeningQuestionnaire:
    row = await db.get(ScreeningQuestionnaire, questionnaire_id)
    if row is None:
        raise NotFound("Questionnaire not found")
    return row


async def version_out(db: AsyncSession, row: ScreeningQuestionnaireVersion) -> VersionOut:
    qn = await db.get(ScreeningQuestionnaire, row.questionnaire_id)
    return VersionOut(
        id=row.id,
        questionnaire_id=row.questionnaire_id,
        questionnaire_name=qn.name if qn is not None else None,
        version=row.version,
        status=row.status,
        questions=[AgentQuestion.model_validate(q) for q in row.questions or []],
        clarifications={k: dict(v) for k, v in (row.clarifications or {}).items()},
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        change_note=row.change_note,
        created_at=row.created_at,
    )


async def list_versions(
    ctx: RequestContext, db: AsyncSession, questionnaire_id: uuid.UUID
) -> list[VersionOut]:
    await _questionnaire(db, questionnaire_id)
    rows = (
        await db.execute(
            select(ScreeningQuestionnaireVersion)
            .where(ScreeningQuestionnaireVersion.questionnaire_id == questionnaire_id)
            .order_by(ScreeningQuestionnaireVersion.version.desc())
        )
    ).scalars()
    return [await version_out(db, r) for r in rows]


async def _version(
    db: AsyncSession, version_id: uuid.UUID, *, for_update: bool = False
) -> ScreeningQuestionnaireVersion:
    stmt = select(ScreeningQuestionnaireVersion).where(ScreeningQuestionnaireVersion.id == version_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Questionnaire version not found")
    return row


async def _latest(db: AsyncSession, questionnaire_id: uuid.UUID) -> ScreeningQuestionnaireVersion | None:
    return (
        await db.execute(
            select(ScreeningQuestionnaireVersion)
            .where(ScreeningQuestionnaireVersion.questionnaire_id == questionnaire_id)
            .order_by(ScreeningQuestionnaireVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def approved_version(
    db: AsyncSession, questionnaire_id: uuid.UUID
) -> ScreeningQuestionnaireVersion | None:
    return (
        await db.execute(
            select(ScreeningQuestionnaireVersion).where(
                ScreeningQuestionnaireVersion.questionnaire_id == questionnaire_id,
                ScreeningQuestionnaireVersion.status == "approved",
            )
        )
    ).scalar_one_or_none()


# ================================================================ writes
async def _next_version_no(db: AsyncSession, questionnaire_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.max(ScreeningQuestionnaireVersion.version)).where(
                ScreeningQuestionnaireVersion.questionnaire_id == questionnaire_id
            )
        )
    ).scalar_one()
    return int(current or 0) + 1


async def create_version(
    ctx: RequestContext, db: AsyncSession, questionnaire_id: uuid.UUID, data: VersionCreate
) -> VersionOut:
    qn = await _questionnaire(db, questionnaire_id)
    if qn.archived_at is not None:
        raise conflict("questionnaire_archived", "The questionnaire is archived")
    latest = await _latest(db, questionnaire_id)
    if data.questions is not None:
        questions = validate_questions(questionnaire_id, data.questions)
    elif latest is not None:
        questions = [dict(q) for q in latest.questions]
    else:
        questions = from_p1(questionnaire_id, list(qn.questions or []), "manual")
    clarifications = (
        {k: dict(v) for k, v in data.clarifications.items()}
        if data.clarifications is not None
        else (
            dict(latest.clarifications)
            if latest is not None and latest.clarifications
            else DEFAULT_CLARIFICATIONS
        )
    )
    row = ScreeningQuestionnaireVersion(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        questionnaire_id=questionnaire_id,
        version=await _next_version_no(db, questionnaire_id),
        questions=questions,
        clarifications=clarifications,
        status="draft",
        change_note=data.change_note,
    )
    db.add(row)
    await db.flush()
    del ctx
    return await version_out(db, row)


async def update_version(
    ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID, data: VersionUpdate
) -> VersionOut:
    row = await _version(db, version_id, for_update=True)
    if row.status != "draft":
        raise conflict("version_not_draft", "Only a draft version can be edited")
    if data.questions is not None:
        row.questions = validate_questions(row.questionnaire_id, data.questions)
    if data.clarifications is not None:
        row.clarifications = {k: dict(v) for k, v in data.clarifications.items()}
    if data.change_note is not None:
        row.change_note = data.change_note
    await db.flush()
    del ctx
    return await version_out(db, row)


async def approve_version(ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID) -> VersionOut:
    """Human only. Every AI-generated question must have been accepted; the previous approved version retires."""
    require_human(actor_from_ctx(ctx))
    row = await _version(db, version_id, for_update=True)
    if row.status != "draft":
        raise conflict("version_not_draft", "Only a draft version can be approved")
    pending = [
        q["key"] for q in row.questions if q.get("origin") == "ai_generated" and not q.get("accepted_by")
    ]
    if pending:
        raise invalid(
            "question_not_accepted",
            "Accept or remove the AI-suggested questions first",
            fields=dict.fromkeys(pending, "Not accepted"),
        )
    if not row.questions:
        raise invalid("questions_required", "The version has no questions")
    previous = await approved_version(db, row.questionnaire_id)
    if previous is not None:
        previous.status = "retired"
        await db.flush()
    row.status = "approved"
    row.approved_by = user_id_of(ctx)
    row.approved_at = now()
    await db.flush()
    audit.record(
        "screening_questionnaire.version_approved",
        "screening_questionnaire_version",
        row.id,
        session=db,
        version=row.version,
    )
    return await version_out(db, row)


async def _mutate_question(
    ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID, question_id: uuid.UUID, *, remove: bool
) -> VersionOut:
    row = await _version(db, version_id, for_update=True)
    if row.status != "draft":
        raise conflict("version_not_draft", "Only a draft version can be edited")
    questions = [dict(q) for q in row.questions]
    found = next((q for q in questions if q["id"] == str(question_id)), None)
    if found is None:
        raise NotFound("Question not found")
    if remove:
        questions = [q for q in questions if q["id"] != str(question_id)]
    else:
        found["accepted_by"] = str(user_id_of(ctx) or ctx.actor_id or uuid.UUID(int=0))
    row.questions = questions
    await db.flush()
    return await version_out(db, row)


async def accept_question(
    ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID, question_id: uuid.UUID
) -> VersionOut:
    require_human(actor_from_ctx(ctx))
    return await _mutate_question(ctx, db, version_id, question_id, remove=False)


async def remove_question(
    ctx: RequestContext, db: AsyncSession, version_id: uuid.UUID, question_id: uuid.UUID
) -> VersionOut:
    return await _mutate_question(ctx, db, version_id, question_id, remove=True)


async def generate_questions(
    ctx: RequestContext, db: AsyncSession, questionnaire_id: uuid.UUID, demand_id: uuid.UUID
) -> GeneratedQuestionsOut:
    """P1 ``screening_questions.v1`` through the gateway (demand data only). Suggestions are ``ai_generated`` and
    need a person's acceptance before the version can be approved. 503 ``ai_unavailable``."""
    from app.schemas.pipeline import QuestionSet
    from app.services import ai
    from app.services.demands import pipeline_port as demands_port
    from app.services.pipeline.common import ServiceUnavailable

    await _questionnaire(db, questionnaire_id)
    demand = await demands_port.get_row(db, demand_id)
    skills = await demands_port.skills(db, demand.id)
    role = {
        "title": demand.title,
        "jd_text": ((await demands_port.jd_text(db, demand)) or "")[:12000],
        "required_skills": [s["label"] for s in skills],
        "must_haves": [s["label"] for s in skills if s["kind"] == "mandatory"],
        "min_experience_years": float(demand.min_experience_years) if demand.min_experience_years else None,
    }
    result = await ai.run(
        ctx, db, "ai_screening", "screening_questions.v1", inputs={},
        untrusted={"role": json.dumps(role, ensure_ascii=False, indent=1)}, output_schema=QuestionSet,
        subject=None, input_ref=f"demand:{demand.id}",
    )  # fmt: skip
    if not result.ok or not isinstance(result.data, QuestionSet):
        raise ServiceUnavailable(
            "AI question generation is unavailable",
            code="ai_unavailable",
            details={"reason": result.error_code},
        )
    out: list[AgentQuestion] = []
    seen: set[str] = set()
    for q in result.data.questions:
        if q.key in seen or (q.type == "choice" and not q.options):
            continue
        seen.add(q.key)
        out.append(
            AgentQuestion(
                id=uuid.uuid4(), key=q.key, label=q.label, type=q.type, options=q.options, required=q.required,
                origin="ai_generated", accepted_by=None,
            )
        )  # fmt: skip
    return GeneratedQuestionsOut(questions=out)


# ================================================================ bindings
def _binding_out(row: ScreeningQuestionnaireBinding) -> BindingOut:
    return BindingOut(
        id=row.id, questionnaire_id=row.questionnaire_id, scope_kind=row.scope_kind, scope_id=row.scope_id
    )


async def list_bindings(
    ctx: RequestContext, db: AsyncSession, questionnaire_id: uuid.UUID
) -> list[BindingOut]:
    await _questionnaire(db, questionnaire_id)
    rows = (
        await db.execute(
            select(ScreeningQuestionnaireBinding)
            .where(ScreeningQuestionnaireBinding.questionnaire_id == questionnaire_id)
            .order_by(ScreeningQuestionnaireBinding.created_at)
        )
    ).scalars()
    return [_binding_out(r) for r in rows]


async def _scope_binding(
    db: AsyncSession, scope_kind: str, scope_id: uuid.UUID | None
) -> ScreeningQuestionnaireBinding | None:
    stmt = select(ScreeningQuestionnaireBinding).where(ScreeningQuestionnaireBinding.scope_kind == scope_kind)
    stmt = stmt.where(
        ScreeningQuestionnaireBinding.scope_id.is_(None)
        if scope_id is None
        else ScreeningQuestionnaireBinding.scope_id == scope_id
    )
    return (await db.execute(stmt.with_for_update())).scalar_one_or_none()


async def bind(
    ctx: RequestContext, db: AsyncSession, questionnaire_id: uuid.UUID, data: BindingIn
) -> BindingOut:
    """Bind (or re-bind) a scope to this questionnaire: one binding per tenant / client / demand."""
    qn = await _questionnaire(db, questionnaire_id)
    if qn.archived_at is not None:
        raise conflict("questionnaire_archived", "The questionnaire is archived")
    if (
        data.scope_kind == "demand"
        and data.scope_id is not None
        and await db.get(Demand, data.scope_id) is None
    ):
        raise NotFound("Demand not found")
    if data.scope_kind == "client" and data.scope_id is not None:
        from app.models.clients import Client

        if await db.get(Client, data.scope_id) is None:
            raise NotFound("Client not found")
    row = await _scope_binding(db, data.scope_kind, data.scope_id)
    if row is None:
        row = ScreeningQuestionnaireBinding(
            id=uuid.uuid4(), tenant_id=tenant_of(db), questionnaire_id=questionnaire_id,
            scope_kind=data.scope_kind, scope_id=data.scope_id,
        )  # fmt: skip
        db.add(row)
    else:
        row.questionnaire_id = questionnaire_id
    await db.flush()
    del ctx
    return _binding_out(row)


async def unbind(
    ctx: RequestContext,
    db: AsyncSession,
    questionnaire_id: uuid.UUID,
    scope_kind: str,
    scope_id: uuid.UUID | None,
) -> None:
    del ctx
    stmt = delete(ScreeningQuestionnaireBinding).where(
        ScreeningQuestionnaireBinding.questionnaire_id == questionnaire_id,
        ScreeningQuestionnaireBinding.scope_kind == scope_kind,
    )
    stmt = stmt.where(
        ScreeningQuestionnaireBinding.scope_id.is_(None)
        if scope_id is None
        else ScreeningQuestionnaireBinding.scope_id == scope_id
    )
    result = await db.execute(stmt)
    if not getattr(result, "rowcount", 0):
        raise NotFound("Binding not found")


# ================================================================ resolution
async def _bound_approved(
    db: AsyncSession, scope_kind: str, scope_id: uuid.UUID | None
) -> ScreeningQuestionnaireVersion | None:
    stmt = select(ScreeningQuestionnaireVersion).join(
        ScreeningQuestionnaireBinding,
        ScreeningQuestionnaireBinding.questionnaire_id == ScreeningQuestionnaireVersion.questionnaire_id,
    ).join(
        ScreeningQuestionnaire, ScreeningQuestionnaire.id == ScreeningQuestionnaireVersion.questionnaire_id
    ).where(
        ScreeningQuestionnaireVersion.status == "approved",
        ScreeningQuestionnaireBinding.scope_kind == scope_kind,
        ScreeningQuestionnaire.archived_at.is_(None),
    )  # fmt: skip
    stmt = stmt.where(
        ScreeningQuestionnaireBinding.scope_id.is_(None)
        if scope_id is None
        else ScreeningQuestionnaireBinding.scope_id == scope_id
    )
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def resolve_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, *, create: bool = True
) -> ScreeningQuestionnaireVersion | None:
    """The approved version used for an Application: demand -> client -> tenant binding; else (``create``) an
    auto-approved demand questionnaire from the P1 defaults + the demand's skill questions."""
    from app.services.pipeline import screening as p1

    app = await db.get(Application, application_id)
    if app is None:
        raise NotFound("Application not found")
    demand = await db.get(Demand, app.demand_id)
    if demand is None:
        raise NotFound("Demand not found")
    for kind, scope in (("demand", demand.id), ("client", demand.client_id), ("tenant", None)):
        found = await _bound_approved(db, kind, scope)
        if found is not None:
            return found
    if not create:
        return None
    await p1.ensure_default_questionnaire(db)
    questions = [dict(q) for q in p1.DEFAULT_QUESTIONS]
    keys = {q["key"] for q in questions}
    skills = [
        dict(q, origin="skill") for q in await p1.skill_questions(db, demand.id) if q["key"] not in keys
    ]
    qn = ScreeningQuestionnaire(
        id=uuid.uuid4(), tenant_id=tenant_of(db), name=f"{AUTO_NAME}: {demand.title}"[:200],
        questions=[dict(q) for q in p1.DEFAULT_QUESTIONS] + [{k: v for k, v in q.items() if k != "origin"} for q in skills],
        is_default=False,
    )  # fmt: skip
    db.add(qn)
    await db.flush()
    agent_questions = from_p1(qn.id, [*[dict(q, origin="default") for q in questions], *skills], "default")
    row = ScreeningQuestionnaireVersion(
        id=uuid.uuid4(), tenant_id=qn.tenant_id, questionnaire_id=qn.id, version=1, questions=agent_questions,
        clarifications=DEFAULT_CLARIFICATIONS, status="approved", approved_by=app.owner_id, approved_at=now(),
        change_note="Created automatically from the default questions and the demand's skills.",
    )  # fmt: skip
    db.add(row)
    existing = await _scope_binding(db, "demand", demand.id)
    if existing is not None:  # bound to a questionnaire without an approved version: re-point it
        existing.questionnaire_id = qn.id
    else:
        db.add(
            ScreeningQuestionnaireBinding(
                id=uuid.uuid4(),
                tenant_id=qn.tenant_id,
                questionnaire_id=qn.id,
                scope_kind="demand",
                scope_id=demand.id,
            )
        )
    await db.flush()
    audit.record(
        "screening_questionnaire.default_approved", "screening_questionnaire_version", row.id, session=db,
        demand_id=str(demand.id),
    )  # fmt: skip
    del ctx
    return row


__all__ = [
    "DEFAULT_CLARIFICATIONS",
    "accept_question",
    "approve_version",
    "approved_version",
    "bind",
    "create_version",
    "from_p1",
    "generate_questions",
    "knockout_hit",
    "list_bindings",
    "list_versions",
    "remove_question",
    "resolve_for_application",
    "stable_id",
    "unbind",
    "update_version",
    "validate_questions",
    "version_out",
]
