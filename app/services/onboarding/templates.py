# ruff: file-ignore[line-too-long, unused-function-argument]
"""Onboarding templates and settings (Epic 23 A1 / A3, P2-10.1, FR-122). ``onboarding:configure``, tenant scope.

Editing a template (fields or tasks) bumps ``version``; existing cases keep their ``template_snapshot``.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound, ValidationFailed
from app.models.onboarding import OnboardingTemplate, OnboardingTemplateTask
from app.schemas.onboarding import (
    OnboardingSettingsOut,
    OnboardingSettingsUpdate,
    TemplateIn,
    TemplateMatchOut,
    TemplateOut,
    TemplatePatch,
    TemplateTaskIn,
    TemplateTaskOut,
    TemplateTasksPut,
)
from app.services.onboarding.common import get_settings_row, now, tenant_of

INTERNAL_TYPES = ("equipment", "system_access", "client_badge", "induction", "other")
DECLARATION_MODES = ("form", "upload", "either")
FORM_FIELD_TYPES = ("text", "number", "date", "choice", "boolean", "file")


# ---------------------------------------------------------------- settings
async def get_settings(ctx: RequestContext, db: AsyncSession) -> OnboardingSettingsOut:
    return OnboardingSettingsOut.model_validate(await get_settings_row(db))


async def update_settings(
    ctx: RequestContext,
    db: AsyncSession,
    data: OnboardingSettingsUpdate,
) -> OnboardingSettingsOut:
    row = await get_settings_row(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if key == "default_owner_by_role":
            value = {k: str(v) if v else None for k, v in (value or {}).items()}
        elif value is None and key in ("auto_invite", "worker_reminder_every_days", "reuse_validity_days"):
            continue
        setattr(row, key, value)
    await db.flush()
    return OnboardingSettingsOut.model_validate(row)


# ---------------------------------------------------------------- validation
def _field_error(index: int, field: str, message: str, code: str) -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"field": f"tasks[{index}].{field}"})


async def _check_document_template(db: AsyncSession, template_id: Any) -> None:
    from app.services import documents_service

    try:
        await documents_service.templates.published_version(db, uuid.UUID(str(template_id)))
    except (DomainError, ValueError, TypeError) as exc:
        raise ValidationFailed(
            "The document template does not exist or has no published version",
            code="document_template_not_found",
            details={"document_template_id": str(template_id)},
        ) from exc


async def validate_tasks(db: AsyncSession, tasks: Sequence[TemplateTaskIn]) -> None:
    seen: set[str] = set()
    for i, t in enumerate(tasks):
        if t.key in seen:
            raise _field_error(i, "key", "Task keys must be unique within a template", "duplicate_task_key")
        seen.add(t.key)
        cfg = t.config or {}
        if t.kind in ("esign", "policy_ack"):
            if not cfg.get("document_template_id"):
                raise _field_error(i, "config", "Pick a document template", "document_template_not_found")
            await _check_document_template(db, cfg["document_template_id"])
        elif t.kind == "credential":
            if not str(cfg.get("credential_type_key") or "").strip():
                raise _field_error(i, "config", "Enter the credential type key", "credential_type_required")
        elif t.kind == "internal":
            if t.owner_kind is None:
                raise _field_error(i, "owner_kind", "Internal tasks need an owner", "owner_required")
            if t.owner_kind == "role" and not t.owner_role_key:
                raise _field_error(i, "owner_role_key", "Pick the owning role", "owner_required")
            if cfg.get("internal_type", "other") not in INTERNAL_TYPES:
                raise _field_error(i, "config", "Unknown internal task type", "invalid_config")
            if t.assignee == "worker":
                raise _field_error(i, "assignee", "Internal tasks are done by users", "invalid_assignee")
        elif t.kind == "document_upload":
            if not str(cfg.get("document_category") or "").strip():
                raise _field_error(i, "config", "Pick the document category", "invalid_config")
        elif t.kind == "tax_details":
            if cfg.get("declaration_mode", "either") not in DECLARATION_MODES:
                raise _field_error(i, "config", "Unknown declaration mode", "invalid_config")
        elif t.kind == "client_form":
            fields = cfg.get("fields")
            if not isinstance(fields, list) or not fields:
                raise _field_error(i, "config", "A client form needs at least one field", "invalid_config")
            names: set[str] = set()
            for f in fields:
                if not isinstance(f, dict) or not f.get("name") or f.get("type") not in FORM_FIELD_TYPES:
                    raise _field_error(
                        i, "config", "Each form field needs a name and a known type", "invalid_config"
                    )
                if f["name"] in names:
                    raise _field_error(i, "config", "Form field names must be unique", "invalid_config")
                names.add(f["name"])
        if t.kind != "internal" and t.assignee == "user":
            raise _field_error(i, "assignee", "Only internal tasks are assigned to users", "invalid_assignee")


def _task_row(
    template_id: uuid.UUID, tenant_id: uuid.UUID, t: TemplateTaskIn, position: int
) -> OnboardingTemplateTask:
    return OnboardingTemplateTask(
        tenant_id=tenant_id,
        template_id=template_id,
        key=t.key,
        position=t.position if t.position is not None else position,
        kind=t.kind,
        label=t.label,
        instructions=t.instructions,
        mandatory=t.mandatory,
        assignee="user" if t.kind == "internal" else "worker",
        owner_kind=t.owner_kind if t.kind == "internal" else None,
        owner_role_key=t.owner_role_key if t.kind == "internal" else None,
        due_offset_days=t.due_offset_days,
        auto_approve=t.auto_approve,
        reusable=False if t.kind == "esign" else t.reusable,  # eSign is never reused
        validity_days=t.validity_days,
        config=dict(t.config or {}),
    )


# ---------------------------------------------------------------- reads
async def _row(db: AsyncSession, template_id: uuid.UUID) -> OnboardingTemplate:
    row = await db.get(OnboardingTemplate, template_id)
    if row is None:
        raise NotFound("Onboarding template not found")
    return row


async def template_tasks(db: AsyncSession, template_id: uuid.UUID) -> list[OnboardingTemplateTask]:
    return list(
        (
            await db.execute(
                select(OnboardingTemplateTask)
                .where(OnboardingTemplateTask.template_id == template_id)
                .order_by(OnboardingTemplateTask.position, OnboardingTemplateTask.key)
            )
        ).scalars()
    )


async def _out(db: AsyncSession, row: OnboardingTemplate, *, with_tasks: bool = True) -> TemplateOut:
    out = TemplateOut.model_validate(row)
    if with_tasks:
        out.tasks = [TemplateTaskOut.model_validate(t) for t in await template_tasks(db, row.id)]
    return out


async def list_templates(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    include_archived: bool = False,
) -> list[TemplateOut]:
    stmt = select(OnboardingTemplate).order_by(OnboardingTemplate.name)
    if not include_archived:
        stmt = stmt.where(OnboardingTemplate.archived_at.is_(None))
    return [await _out(db, r) for r in (await db.execute(stmt)).scalars()]


async def get_template(ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID) -> TemplateOut:
    return await _out(db, await _row(db, template_id))


# ---------------------------------------------------------------- writes
async def create_template(ctx: RequestContext, db: AsyncSession, data: TemplateIn) -> TemplateOut:
    await validate_tasks(db, data.tasks)
    tenant_id = tenant_of(db)
    row = OnboardingTemplate(
        tenant_id=tenant_id,
        name=data.name,
        legal_entity_id=data.legal_entity_id,
        client_id=data.client_id,
        worker_type=data.worker_type,
        country=data.country,
        active=data.active,
        version=1,
    )
    db.add(row)
    await db.flush()
    for i, t in enumerate(data.tasks):
        db.add(_task_row(row.id, tenant_id, t, i))
    await db.flush()
    return await _out(db, row)


async def patch_template(
    ctx: RequestContext,
    db: AsyncSession,
    template_id: uuid.UUID,
    data: TemplatePatch,
) -> TemplateOut:
    row = await _row(db, template_id)
    changes = data.model_dump(exclude_unset=True)
    archived = changes.pop("archived", None)
    for key, value in changes.items():
        if key in ("name", "active") and value is None:
            continue
        setattr(row, key, value)
    if archived is not None:
        row.archived_at = now() if archived else None
        if archived:
            row.active = False
    row.version = (row.version or 1) + 1
    await db.flush()
    return await _out(db, row)


async def put_tasks(
    ctx: RequestContext,
    db: AsyncSession,
    template_id: uuid.UUID,
    data: TemplateTasksPut,
) -> TemplateOut:
    row = await _row(db, template_id)
    await validate_tasks(db, data.tasks)
    await db.execute(delete(OnboardingTemplateTask).where(OnboardingTemplateTask.template_id == row.id))
    for i, t in enumerate(data.tasks):
        db.add(_task_row(row.id, row.tenant_id, t, i))
    row.version = (row.version or 1) + 1
    await db.flush()
    return await _out(db, row)


async def add_task(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, data: TemplateTaskIn
) -> TemplateOut:
    existing = await template_tasks(db, template_id)
    ins = [_as_in(t) for t in existing] + [data]
    return await put_tasks(ctx, db, template_id, TemplateTasksPut(tasks=ins))


async def delete_task(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, task_id: uuid.UUID
) -> TemplateOut:
    existing = await template_tasks(db, template_id)
    if not any(t.id == task_id for t in existing):
        raise NotFound("Template task not found")
    ins = [_as_in(t) for t in existing if t.id != task_id]
    return await put_tasks(ctx, db, template_id, TemplateTasksPut(tasks=ins))


def _as_in(t: OnboardingTemplateTask) -> TemplateTaskIn:
    return TemplateTaskIn(
        key=t.key,
        kind=t.kind,  # type: ignore[arg-type]
        label=t.label,
        instructions=t.instructions,
        mandatory=t.mandatory,
        assignee=t.assignee,  # type: ignore[arg-type]
        owner_kind=t.owner_kind,  # type: ignore[arg-type]
        owner_role_key=t.owner_role_key,
        due_offset_days=t.due_offset_days,
        auto_approve=t.auto_approve,
        reusable=t.reusable,
        validity_days=t.validity_days,
        config=dict(t.config or {}),
        position=t.position,
    )


async def preview_match(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> TemplateMatchOut:
    from app.services.onboarding import cases

    facts = await cases.placement_facts(ctx, db, placement_id)
    template = await cases.select_template(db, facts)
    return TemplateMatchOut(
        placement_id=placement_id,
        template_id=template.id if template else None,
        template_name=template.name if template else None,
    )


def snapshot(template: OnboardingTemplate, tasks: Sequence[OnboardingTemplateTask]) -> dict[str, Any]:
    return {
        "id": str(template.id),
        "name": template.name,
        "version": template.version,
        "match": {
            "legal_entity_id": str(template.legal_entity_id) if template.legal_entity_id else None,
            "client_id": str(template.client_id) if template.client_id else None,
            "worker_type": template.worker_type,
            "country": template.country,
        },
        "tasks": [
            {
                "key": t.key,
                "kind": t.kind,
                "label": t.label,
                "mandatory": t.mandatory,
                "assignee": t.assignee,
                "owner_kind": t.owner_kind,
                "owner_role_key": t.owner_role_key,
                "due_offset_days": t.due_offset_days,
                "auto_approve": t.auto_approve,
                "reusable": t.reusable,
                "validity_days": t.validity_days,
                "config": dict(t.config or {}),
            }
            for t in tasks
        ],
    }
