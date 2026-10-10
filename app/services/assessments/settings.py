"""Assessment settings (Epic 40 A1): one ``assessment_settings`` row per tenant, created lazily."""

import dataclasses
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.assessments import AssessmentSettings
from app.schemas.assessments import SettingsIn, SettingsOut
from app.services.assessments.common import tenant_of


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    allow_auto_pass_non_gating: bool = False
    short_answer_confidence_threshold: Decimal = Decimal("0.800")
    reminder_hours_before: int = 24
    default_deadline_days: int = 5
    link_grace_days: int = 1
    max_signals_per_minute: int = 60
    max_signals_per_attempt: int = 2000
    multi_choice_partial_credit: bool = False


FIELDS = tuple(f.name for f in dataclasses.fields(Settings))


async def current(db: AsyncSession) -> Settings:
    row = (await db.execute(select(AssessmentSettings))).scalar_one_or_none()
    if row is None:
        return Settings()
    return Settings(**{f: getattr(row, f) for f in FIELDS})


def _out(s: Settings) -> SettingsOut:
    return SettingsOut(**dataclasses.asdict(s))


async def get(ctx: RequestContext, db: AsyncSession) -> SettingsOut:  # noqa: ARG001
    return _out(await current(db))


async def update(ctx: RequestContext, db: AsyncSession, data: SettingsIn) -> SettingsOut:  # noqa: ARG001
    tenant_id: uuid.UUID = tenant_of(db)
    await db.execute(
        insert(AssessmentSettings)
        .values(id=uuid.uuid4(), tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    row = (
        await db.execute(select(AssessmentSettings).execution_options(populate_existing=True))
    ).scalar_one()
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    return _out(await current(db))
