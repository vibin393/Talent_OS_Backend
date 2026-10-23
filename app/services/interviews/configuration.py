"""Tenant interview configuration (7.1, FR-41): settings, interview kits and stage templates.

* Editing a kit bumps ``version``; scorecards snapshot the kit so later edits never change them.
* One default stage template per tenant ("Technical -> Client -> HR"): seeded by migration 70 for existing
  tenants and lazily (``ensure_default_template``) for new ones.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import check
from app.models.interviews import InterviewKit, InterviewStageTemplate
from app.schemas.interviews import (
    InterviewSettingsOut,
    InterviewSettingsUpdate,
    KitCreate,
    KitOut,
    KitUpdate,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
)
from app.services.interviews.common import (
    CONFIGURE,
    DEFAULT_TEMPLATE_NAME,
    DEFAULT_TEMPLATE_STAGES,
    RECOMMENDATION_OPTIONS,
    now,
    principal_of,
    settings_row,
    tenant_of,
)


# ================================================================ settings
def settings_out(row: Any) -> InterviewSettingsOut:
    return InterviewSettingsOut(
        reminder_offsets_minutes=list(row.reminder_offsets_minutes or []),
        scorecard_reminder_after_hours=row.scorecard_reminder_after_hours,
        external_scorecard_link_days_after=row.external_scorecard_link_days_after,
        default_duration_minutes=row.default_duration_minutes,
        rating_scale=row.rating_scale,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> InterviewSettingsOut:
    check(principal_of(ctx), CONFIGURE)
    return settings_out(await settings_row(db))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: InterviewSettingsUpdate
) -> InterviewSettingsOut:
    check(principal_of(ctx), CONFIGURE)
    row = await settings_row(db)
    changes = data.model_dump(exclude_unset=True)
    if "reminder_offsets_minutes" in changes and changes["reminder_offsets_minutes"] is not None:
        changes["reminder_offsets_minutes"] = sorted(set(changes["reminder_offsets_minutes"]), reverse=True)
    for key, value in changes.items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    return settings_out(row)


# ================================================================ kits
def kit_out(row: InterviewKit) -> KitOut:
    return KitOut(
        id=row.id,
        name=row.name,
        description=row.description,
        competencies=row.competencies or [],
        questions=row.questions or [],
        rating_scale=row.rating_scale,
        recommendation_options=list(row.recommendation_options or RECOMMENDATION_OPTIONS),
        version=row.version,
        archived_at=row.archived_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def kit_snapshot(row: InterviewKit | None, rating_scale: int) -> dict[str, Any]:
    """Frozen copy stored on interviews and scorecards (no kit = one overall competency)."""
    if row is None:
        return {
            "kit_id": None,
            "name": None,
            "version": 0,
            "competencies": [{"key": "overall", "label": "Overall", "guidance": None}],
            "questions": [],
            "rating_scale": rating_scale,
            "recommendation_options": list(RECOMMENDATION_OPTIONS),
        }
    return {
        "kit_id": str(row.id),
        "name": row.name,
        "version": row.version,
        "competencies": list(row.competencies or []),
        "questions": list(row.questions or []),
        "rating_scale": row.rating_scale,
        "recommendation_options": list(row.recommendation_options or RECOMMENDATION_OPTIONS),
    }


def _validate_kit(competencies: list[dict[str, Any]], questions: list[dict[str, Any]]) -> None:
    keys = [c["key"] for c in competencies]
    if len(set(keys)) != len(keys):
        raise ValidationFailed("Competency keys must be unique", code="duplicate_competency")
    qkeys = [q["key"] for q in questions]
    if len(set(qkeys)) != len(qkeys):
        raise ValidationFailed("Question keys must be unique", code="duplicate_question")
    for q in questions:
        if q.get("competency_key") and q["competency_key"] not in keys:
            raise ValidationFailed(
                "A question references an unknown competency",
                code="unknown_competency",
                details={"question_key": q["key"]},
            )


async def list_kits(ctx: RequestContext, db: AsyncSession, include_archived: bool = False) -> list[KitOut]:
    principal_of(ctx)
    stmt = select(InterviewKit).order_by(InterviewKit.name, InterviewKit.id)
    if not include_archived:
        stmt = stmt.where(InterviewKit.archived_at.is_(None))
    return [kit_out(r) for r in (await db.execute(stmt)).scalars()]


async def _kit(db: AsyncSession, kit_id: uuid.UUID) -> InterviewKit:
    row = await db.get(InterviewKit, kit_id)
    if row is None:
        raise NotFound("Interview kit not found")
    return row


async def get_kit(ctx: RequestContext, db: AsyncSession, kit_id: uuid.UUID) -> KitOut:
    principal_of(ctx)
    return kit_out(await _kit(db, kit_id))


async def create_kit(ctx: RequestContext, db: AsyncSession, data: KitCreate) -> KitOut:
    check(principal_of(ctx), CONFIGURE)
    competencies = [c.model_dump() for c in data.competencies]
    questions = [q.model_dump() for q in data.questions]
    _validate_kit(competencies, questions)
    row = InterviewKit(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name,
        description=data.description,
        competencies=competencies,
        questions=questions,
        rating_scale=data.rating_scale,
        recommendation_options=list(RECOMMENDATION_OPTIONS),
        version=1,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return kit_out(row)


async def update_kit(ctx: RequestContext, db: AsyncSession, kit_id: uuid.UUID, data: KitUpdate) -> KitOut:
    check(principal_of(ctx), CONFIGURE)
    row = await _kit(db, kit_id)
    changes = data.model_dump(exclude_unset=True)
    competencies = changes.get("competencies") or list(row.competencies or [])
    questions = changes["questions"] if changes.get("questions") is not None else list(row.questions or [])
    _validate_kit(competencies, questions)
    for key in ("name", "description", "rating_scale"):
        if key in changes and (changes[key] is not None or key == "description"):
            setattr(row, key, changes[key])
    if changes.get("competencies") is not None:
        row.competencies = competencies
    if changes.get("questions") is not None:
        row.questions = questions
    row.version = row.version + 1
    await db.flush()
    await db.refresh(row)
    return kit_out(row)


async def archive_kit(ctx: RequestContext, db: AsyncSession, kit_id: uuid.UUID) -> KitOut:
    check(principal_of(ctx), CONFIGURE)
    row = await _kit(db, kit_id)
    if row.archived_at is None:
        row.archived_at = now()
        await db.flush()
        await db.refresh(row)
    return kit_out(row)


async def active_kit_ids(db: AsyncSession, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    rows = await db.execute(
        select(InterviewKit.id).where(InterviewKit.id.in_(ids), InterviewKit.archived_at.is_(None))
    )
    return set(rows.scalars())


# ================================================================ stage templates
def template_out(row: InterviewStageTemplate) -> TemplateOut:
    return TemplateOut(
        id=row.id,
        name=row.name,
        stages=row.stages or [],
        is_default=row.is_default,
        archived_at=row.archived_at,
        created_at=row.created_at,
    )


async def ensure_default_template(db: AsyncSession) -> None:
    """Seed the tenant default "Technical -> Client -> HR" template once (new tenants; idempotent)."""
    exists = (await db.execute(select(InterviewStageTemplate.id).limit(1))).first()
    if exists is not None:
        return
    db.add(
        InterviewStageTemplate(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            name=DEFAULT_TEMPLATE_NAME,
            stages=[dict(s) for s in DEFAULT_TEMPLATE_STAGES],
            is_default=True,
        )
    )
    await db.flush()


async def default_template(db: AsyncSession) -> InterviewStageTemplate | None:
    await ensure_default_template(db)
    return (
        await db.execute(
            select(InterviewStageTemplate)
            .where(InterviewStageTemplate.archived_at.is_(None))
            .order_by(InterviewStageTemplate.is_default.desc(), InterviewStageTemplate.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _check_template_kits(db: AsyncSession, stages: list[dict[str, Any]]) -> None:
    ids = {uuid.UUID(str(s["kit_id"])) for s in stages if s.get("kit_id")}
    missing = ids - await active_kit_ids(db, ids)
    if missing:
        raise ValidationFailed(
            "Unknown or archived interview kit",
            code="invalid_kit",
            details={"kit_ids": sorted(map(str, missing))},
        )


def _stages_json(stages: list[Any]) -> list[dict[str, Any]]:
    return [{**s.model_dump(), "kit_id": str(s.kit_id) if s.kit_id else None} for s in stages]


async def _clear_default(db: AsyncSession, keep: uuid.UUID | None) -> None:
    stmt = update(InterviewStageTemplate).where(InterviewStageTemplate.is_default.is_(True))
    if keep is not None:
        stmt = stmt.where(InterviewStageTemplate.id != keep)
    await db.execute(stmt.values(is_default=False))


async def list_templates(
    ctx: RequestContext, db: AsyncSession, include_archived: bool = False
) -> list[TemplateOut]:
    principal_of(ctx)
    await ensure_default_template(db)
    stmt = select(InterviewStageTemplate).order_by(
        InterviewStageTemplate.is_default.desc(), InterviewStageTemplate.name, InterviewStageTemplate.id
    )
    if not include_archived:
        stmt = stmt.where(InterviewStageTemplate.archived_at.is_(None))
    return [template_out(r) for r in (await db.execute(stmt)).scalars()]


async def get_template_row(db: AsyncSession, template_id: uuid.UUID) -> InterviewStageTemplate:
    row = await db.get(InterviewStageTemplate, template_id)
    if row is None:
        raise NotFound("Stage template not found")
    return row


async def get_template(ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID) -> TemplateOut:
    principal_of(ctx)
    return template_out(await get_template_row(db, template_id))


async def create_template(ctx: RequestContext, db: AsyncSession, data: TemplateCreate) -> TemplateOut:
    check(principal_of(ctx), CONFIGURE)
    await ensure_default_template(db)
    stages = _stages_json(data.stages)
    await _check_template_kits(db, stages)
    if data.is_default:
        await _clear_default(db, None)
    row = InterviewStageTemplate(
        id=uuid.uuid4(), tenant_id=tenant_of(db), name=data.name, stages=stages, is_default=data.is_default
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return template_out(row)


async def update_template(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, data: TemplateUpdate
) -> TemplateOut:
    check(principal_of(ctx), CONFIGURE)
    row = await get_template_row(db, template_id)
    if data.name is not None:
        row.name = data.name
    if data.stages is not None:
        stages = _stages_json(data.stages)
        await _check_template_kits(db, stages)
        row.stages = stages
    if data.archived is not None:
        row.archived_at = now() if data.archived else None
        if data.archived:
            row.is_default = False
    if data.is_default is not None:
        if data.is_default:
            if row.archived_at is not None:
                raise ValidationFailed("An archived template cannot be the default", code="template_archived")
            await _clear_default(db, row.id)
        row.is_default = data.is_default
    await db.flush()
    await db.refresh(row)
    return template_out(row)
