# ruff: noqa: E501
"""BGV configuration (Epic 20 A1/A3, ``bgv:configure``): packages, providers, reference questionnaires, settings.

* ``criminal_record`` / ``drug_test`` checks need a lawful-basis attestation by the configuring user (PRD §8.3):
  ``lawful_confirmed=true`` records ``lawful_confirmed_by/at``; without it the package is refused (422
  ``lawful_attestation_required``). Changing the checks of an attested package re-requires the attestation.
* Archiving never touches existing cases (each case keeps ``package_snapshot``).
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.bgv import LAWFUL_TYPES, BgvPackage, BgvProvider, ReferenceQuestionnaire
from app.schemas.bgv import (
    BgvSettingsOut,
    BgvSettingsUpdate,
    PackageIn,
    PackageOut,
    PackageUpdate,
    ProviderIn,
    ProviderOut,
    ProviderUpdate,
    QuestionnaireIn,
    QuestionnaireOut,
    QuestionnaireUpdate,
)
from app.services.bgv.common import now, settings_row, tenant_of, user_id

DEFAULT_QUESTIONS: list[dict[str, Any]] = [
    {
        "key": "relationship",
        "text": "How do you know the candidate, and for how long?",
        "kind": "text",
        "required": True,
    },
    {
        "key": "role",
        "text": "What was the candidate's role and main responsibilities?",
        "kind": "text",
        "required": True,
    },
    {
        "key": "performance",
        "text": "How would you rate their overall performance (1-5)?",
        "kind": "rating",
        "required": True,
    },
    {"key": "rehire", "text": "Would you work with them again?", "kind": "yes_no", "required": True},
    {"key": "comments", "text": "Anything else we should know?", "kind": "text", "required": False},
]


# ================================================================ packages
def _lawful(checks: list[dict[str, Any]]) -> bool:
    return any(c["type"] in LAWFUL_TYPES for c in checks)


async def list_packages(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    include_archived: bool = False,
) -> list[PackageOut]:
    stmt = select(BgvPackage).order_by(BgvPackage.name)
    if not include_archived:
        stmt = stmt.where(BgvPackage.archived_at.is_(None))
    return [PackageOut.model_validate(p) for p in (await db.execute(stmt)).scalars()]


async def get_package(db: AsyncSession, package_id: uuid.UUID) -> BgvPackage:
    row = await db.get(BgvPackage, package_id)
    if row is None:
        raise NotFound("BGV package not found")
    return row


def _attest(
    ctx: RequestContext, row: BgvPackage, checks: list[dict[str, Any]], confirmed: bool | None
) -> None:
    if not _lawful(checks):
        row.lawful_confirmed_by = None
        row.lawful_confirmed_at = None
        return
    if confirmed:
        row.lawful_confirmed_by = user_id(ctx)
        row.lawful_confirmed_at = now()
    if row.lawful_confirmed_by is None:
        raise ValidationFailed(
            "Criminal record and drug test checks need a lawful-basis attestation",
            code="lawful_attestation_required",
            details={"fields": {"lawful_confirmed": "Confirm the lawful basis for these checks"}},
        )


async def create_package(ctx: RequestContext, db: AsyncSession, data: PackageIn) -> PackageOut:
    checks = [c.model_dump() for c in data.checks]
    row = BgvPackage(
        id=uuid.uuid4(),
        tenant_id=tenant_of(ctx, db),
        name=data.name,
        description=data.description,
        checks=checks,
        turnaround_target_days=data.turnaround_target_days,
    )
    _attest(ctx, row, checks, data.lawful_confirmed)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return PackageOut.model_validate(row)


async def update_package(
    ctx: RequestContext, db: AsyncSession, package_id: uuid.UUID, data: PackageUpdate
) -> PackageOut:
    row = await get_package(db, package_id)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("name"):
        row.name = changes["name"]
    if "description" in changes:
        row.description = changes["description"]
    if "turnaround_target_days" in changes:
        row.turnaround_target_days = changes["turnaround_target_days"]
    checks = row.checks
    if data.checks is not None:
        checks = [c.model_dump() for c in data.checks]
        if checks != row.checks:
            row.lawful_confirmed_by = None  # new check list: re-attest
        row.checks = checks
    _attest(ctx, row, checks, data.lawful_confirmed)
    await db.flush()
    await db.refresh(row)
    return PackageOut.model_validate(row)


async def archive_package(ctx: RequestContext, db: AsyncSession, package_id: uuid.UUID) -> PackageOut:  # noqa: ARG001
    row = await get_package(db, package_id)
    if row.archived_at is None:
        row.archived_at = now()
        await db.flush()
    await db.refresh(row)
    return PackageOut.model_validate(row)


# ================================================================ providers
async def list_providers(ctx: RequestContext, db: AsyncSession) -> list[ProviderOut]:  # noqa: ARG001
    rows = (await db.execute(select(BgvProvider).order_by(BgvProvider.name))).scalars()
    return [ProviderOut.model_validate(p) for p in rows]


async def create_provider(ctx: RequestContext, db: AsyncSession, data: ProviderIn) -> ProviderOut:
    row = BgvProvider(id=uuid.uuid4(), tenant_id=tenant_of(ctx, db), **data.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return ProviderOut.model_validate(row)


async def update_provider(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    provider_id: uuid.UUID,
    data: ProviderUpdate,
) -> ProviderOut:
    row = await db.get(BgvProvider, provider_id)
    if row is None:
        raise NotFound("BGV provider not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None and key in ("name", "active", "supported_types"):
            continue
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return ProviderOut.model_validate(row)


# ================================================================ reference questionnaires
async def list_questionnaires(ctx: RequestContext, db: AsyncSession) -> list[QuestionnaireOut]:  # noqa: ARG001
    rows = (await db.execute(select(ReferenceQuestionnaire).order_by(ReferenceQuestionnaire.name))).scalars()
    return [QuestionnaireOut.model_validate(q) for q in rows]


async def _clear_default(db: AsyncSession, keep: uuid.UUID) -> None:
    await db.execute(
        update(ReferenceQuestionnaire).where(ReferenceQuestionnaire.id != keep).values(is_default=False)
    )


async def create_questionnaire(
    ctx: RequestContext, db: AsyncSession, data: QuestionnaireIn
) -> QuestionnaireOut:
    row = ReferenceQuestionnaire(
        id=uuid.uuid4(),
        tenant_id=tenant_of(ctx, db),
        name=data.name,
        questions=[q.model_dump() for q in data.questions],
        is_default=data.is_default,
    )
    db.add(row)
    await db.flush()
    if data.is_default:
        await _clear_default(db, row.id)
    await db.refresh(row)
    return QuestionnaireOut.model_validate(row)


async def update_questionnaire(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    questionnaire_id: uuid.UUID,
    data: QuestionnaireUpdate,
) -> QuestionnaireOut:
    row = await db.get(ReferenceQuestionnaire, questionnaire_id)
    if row is None:
        raise NotFound("Questionnaire not found")
    if data.name:
        row.name = data.name
    if data.questions is not None:
        row.questions = [q.model_dump() for q in data.questions]
    if data.archived is not None:
        row.archived_at = now() if data.archived else None
    if data.is_default is not None:
        row.is_default = data.is_default
    await db.flush()
    if row.is_default:
        await _clear_default(db, row.id)
    await db.refresh(row)
    return QuestionnaireOut.model_validate(row)


async def questionnaire_snapshot(db: AsyncSession, questionnaire_id: uuid.UUID | None) -> dict[str, Any]:
    stmt = select(ReferenceQuestionnaire).where(ReferenceQuestionnaire.archived_at.is_(None))
    if questionnaire_id is not None:
        row = await db.get(ReferenceQuestionnaire, questionnaire_id)
        if row is None:
            raise NotFound("Questionnaire not found")
    else:
        row = (await db.execute(stmt.where(ReferenceQuestionnaire.is_default.is_(True)))).scalars().first()
    if row is None:
        return {"id": None, "name": "Standard reference", "questions": DEFAULT_QUESTIONS}
    return {"id": str(row.id), "name": row.name, "questions": list(row.questions)}


# ================================================================ settings
async def get_settings(ctx: RequestContext, db: AsyncSession) -> BgvSettingsOut:
    return BgvSettingsOut.model_validate(await settings_row(db, tenant_of(ctx, db)))


async def update_settings(ctx: RequestContext, db: AsyncSession, data: BgvSettingsUpdate) -> BgvSettingsOut:
    row = await settings_row(db, tenant_of(ctx, db))
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return BgvSettingsOut.model_validate(row)
