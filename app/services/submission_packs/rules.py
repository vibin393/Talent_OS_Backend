# ruff: noqa: E501
"""Per-Client submission rules (Epic 61 A2). One optional row per Client; absent = defaults. ``effective_rules``
merges the row with the tenant pipeline settings (duplicate window, consent mode) so the pack and the P1 dialog never
disagree."""

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import check
from app.models.submission_packs import ClientSubmissionRules
from app.schemas.submission_packs import ClientSubmissionRulesIn, ClientSubmissionRulesOut
from app.services.submission_packs.common import CONFIGURE, principal, tenant_of

DEFAULTS = ClientSubmissionRulesIn()


@dataclasses.dataclass(frozen=True, slots=True)
class EffectiveRules:
    client_id: uuid.UUID
    resume_format: str
    mask_contact_details: bool
    write_up_max_words: int
    write_up_instructions: str | None
    duplicate_window_days: int
    require_candidate_consent: bool
    show_notice_period: bool
    consent_mode: str
    min_margin_pct: object
    is_default: bool


async def _row(db: AsyncSession, client_id: uuid.UUID) -> ClientSubmissionRules | None:
    return (
        await db.execute(select(ClientSubmissionRules).where(ClientSubmissionRules.client_id == client_id))
    ).scalar_one_or_none()


async def effective_rules(db: AsyncSession, client_id: uuid.UUID) -> EffectiveRules:
    from app.services import pipeline_service

    settings = await pipeline_service.get_pipeline_settings(db)
    row = await _row(db, client_id)
    src = row if row is not None else DEFAULTS
    window = (row.duplicate_window_days if row is not None else None) or settings.prior_submission_window_days
    return EffectiveRules(
        client_id=client_id,
        resume_format=src.resume_format,
        mask_contact_details=src.mask_contact_details,
        write_up_max_words=src.write_up_max_words,
        write_up_instructions=src.write_up_instructions,
        duplicate_window_days=int(window),
        require_candidate_consent=src.require_candidate_consent,
        show_notice_period=src.show_notice_period,
        consent_mode=settings.submission_consent_mode,
        min_margin_pct=settings.min_margin_pct,
        is_default=row is None,
    )


async def _client_exists(db: AsyncSession, client_id: uuid.UUID) -> None:
    from app.services import entity_refs

    if await entity_refs.entity_label(db, "client", client_id) is None:
        raise NotFound("Client not found")


async def _out(db: AsyncSession, client_id: uuid.UUID) -> ClientSubmissionRulesOut:
    from app.services import pipeline_service

    settings = await pipeline_service.get_pipeline_settings(db)
    row = await _row(db, client_id)
    src = row if row is not None else DEFAULTS
    return ClientSubmissionRulesOut(
        client_id=client_id,
        resume_format=src.resume_format,  # type: ignore[arg-type]
        mask_contact_details=src.mask_contact_details,
        write_up_max_words=src.write_up_max_words,
        write_up_instructions=src.write_up_instructions,
        duplicate_window_days=src.duplicate_window_days,
        require_candidate_consent=src.require_candidate_consent,
        show_notice_period=src.show_notice_period,
        is_default=row is None,
        tenant_duplicate_window_days=settings.prior_submission_window_days,
        tenant_consent_mode=settings.submission_consent_mode,
        updated_by=row.updated_by if row is not None else None,
        updated_at=row.updated_at if row is not None else None,
    )


async def get_client_rules(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    client_id: uuid.UUID,
) -> ClientSubmissionRulesOut:
    await _client_exists(db, client_id)
    return await _out(db, client_id)


async def put_client_rules(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientSubmissionRulesIn
) -> ClientSubmissionRulesOut:
    p = principal(ctx)
    if p is not None:
        check(p, CONFIGURE)
    await _client_exists(db, client_id)
    row = await _row(db, client_id)
    if row is None:
        row = ClientSubmissionRules(id=uuid.uuid4(), tenant_id=tenant_of(db), client_id=client_id)
        db.add(row)
    for field, value in data.model_dump().items():
        setattr(row, field, value)
    row.updated_by = getattr(p, "id", None)
    await db.flush()
    audit.record(
        "submission_rules.updated",
        "client",
        client_id,
        session=db,
        **data.model_dump(exclude={"write_up_instructions"}),
    )
    await db.refresh(row)
    return await _out(db, client_id)


__all__ = ["DEFAULTS", "EffectiveRules", "effective_rules", "get_client_rules", "put_client_rules"]
