"""Career site administration (Epic 9 story 9.1, FR-46): branding, about text, stage labels, logo / header
images, and a posting's apply questions.

* One ``career_sites`` row per tenant, created lazily on first read.
* ``brand_color`` must reach 4.5:1 against white, else 422 ``brand_color_low_contrast`` with
  ``details.suggested`` (the same hue darkened until it passes).
* ``about_md`` is rendered with markdown-it (raw HTML off) and sanitised with nh3; ``about_html`` is stored.
* Images: png / jpeg / webp only (SVG and anything else refused by content sniffing), <= 2 MB, scanned.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import NotFound, ValidationFailed
from app.core.settings import get_settings
from app.models.demands import DemandPosting
from app.models.identity import Tenant
from app.models.portal import CareerSite
from app.schemas.pipeline import Question
from app.schemas.portal import CareerSiteOut, CareerSiteUpdate, PostingQuestionsIn, PostingQuestionsOut
from app.services import files_service
from app.services.portal.common import (
    DEFAULT_STAGE_LABELS,
    MIN_CONTRAST,
    contrast_with_white,
    darken_to_contrast,
    render_markdown,
)
from app.services.portal.projection import asset_url

IMAGE_KINDS = frozenset({"png", "jpeg", "webp"})
MAX_IMAGE_MB = 2
IMAGE_OWNER = "career_site"


def _tenant_id(ctx: RequestContext) -> uuid.UUID:
    if ctx.tenant_id is None:
        raise NotFound("Career site not found")
    return ctx.tenant_id


async def _row(db: AsyncSession, tenant_id: uuid.UUID, *, for_update: bool = False) -> CareerSite:
    row = await db.get(CareerSite, tenant_id, with_for_update=for_update)
    if row is None:
        row = CareerSite(tenant_id=tenant_id, candidate_stage_labels={})
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def _slug(tenant_id: uuid.UUID) -> str:
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
    return str(tenant.slug) if tenant is not None else ""


def _out(row: CareerSite, slug: str) -> CareerSiteOut:
    s = get_settings()
    public = (
        f"https://{slug}.{s.careers_root_domain.strip('.')}"
        if s.careers_root_domain
        else f"{s.frontend_url.rstrip('/')}/careers/{slug}"
    )
    return CareerSiteOut(
        slug=slug,
        enabled=row.enabled,
        display_name=row.display_name,
        brand_color=row.brand_color,
        about_md=row.about_md,
        about_html=row.about_html,
        privacy_contact_email=str(row.privacy_contact_email) if row.privacy_contact_email else None,
        candidate_stage_labels={str(k): str(v) for k, v in (row.candidate_stage_labels or {}).items()},
        default_stage_labels=dict(DEFAULT_STAGE_LABELS),
        apply_requires_phone=row.apply_requires_phone,
        logo_url=asset_url(slug, "logo") if row.logo_file_id else None,
        header_url=asset_url(slug, "header") if row.header_file_id else None,
        public_url=public,
        updated_at=row.updated_at,
    )


async def get_site(ctx: RequestContext, db: AsyncSession) -> CareerSiteOut:
    tid = _tenant_id(ctx)
    return _out(await _row(db, tid), await _slug(tid))


def check_brand_color(color: str) -> None:
    ratio = contrast_with_white(color)
    if ratio < MIN_CONTRAST:
        raise ValidationFailed(
            "The brand colour does not have enough contrast against white (4.5:1 required)",
            code="brand_color_low_contrast",
            details={"contrast": round(ratio, 2), "suggested": darken_to_contrast(color)},
        )


async def update_site(ctx: RequestContext, db: AsyncSession, data: CareerSiteUpdate) -> CareerSiteOut:
    tid = _tenant_id(ctx)
    row = await _row(db, tid, for_update=True)
    fields = data.model_fields_set
    if "brand_color" in fields and data.brand_color is not None:
        check_brand_color(data.brand_color)
    for name in ("enabled", "display_name", "brand_color", "apply_requires_phone"):
        if name in fields:
            value = getattr(data, name)
            if name in ("enabled", "apply_requires_phone") and value is None:
                continue
            setattr(row, name, value)
    if "privacy_contact_email" in fields:
        row.privacy_contact_email = (
            str(data.privacy_contact_email).lower() if data.privacy_contact_email else None
        )
    if "about_md" in fields:
        row.about_md = data.about_md or None
        row.about_html = render_markdown(data.about_md) or None
    if "candidate_stage_labels" in fields:
        row.candidate_stage_labels = {k: v for k, v in (data.candidate_stage_labels or {}).items() if v}
    await db.flush()
    await db.refresh(row)
    return _out(row, await _slug(tid))


async def upload_image(
    ctx: RequestContext, db: AsyncSession, which: str, data: bytes, filename: str | None
) -> CareerSiteOut:
    tid = _tenant_id(ctx)
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise ValidationFailed(
            f"The image is larger than {MAX_IMAGE_MB} MB",
            code="file_too_large",
            details={"max_mb": MAX_IMAGE_MB},
        )
    row = await _row(db, tid, for_update=True)
    file = await files_service.store_upload(ctx, db, data, filename, IMAGE_KINDS, IMAGE_OWNER, tid)
    if which == "logo":
        row.logo_file_id = file.id
    else:
        row.header_file_id = file.id
    await db.flush()
    await db.refresh(row)
    return _out(row, await _slug(tid))


async def career_site_access(
    ctx: RequestContext,
    db: AsyncSession,  # noqa: ARG001 - files access hook signature
    entity_id: uuid.UUID,
    write: bool,  # noqa: ARG001 - files access hook signature
) -> bool:
    """Files access hook: career-site images belong to the tenant (served publicly by the careers API)."""
    return ctx.tenant_id == entity_id


# ================================================================ posting apply questions (Epic 4 permission)
async def set_apply_questions(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: PostingQuestionsIn
) -> PostingQuestionsOut:
    from app.services import demands as demands_service

    if not await demands_service.demand_access(ctx, db, demand_id, True):
        raise NotFound("Demand not found")
    posting = (
        await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand_id).with_for_update())
    ).scalar_one_or_none()
    if posting is None:
        raise NotFound("The demand has no posting", code="posting_not_found")
    keys = [q.key for q in data.apply_questions]
    if len(keys) != len(set(keys)):
        raise ValidationFailed("Question keys must be unique", code="duplicate_question_key")
    posting.apply_questions = [q.model_dump(mode="json", exclude_none=True) for q in data.apply_questions]
    await db.flush()
    return PostingQuestionsOut(
        demand_id=demand_id,
        posting_id=posting.id,
        apply_questions=[Question.model_validate(q) for q in posting.apply_questions],
    )


def summary(row: Any) -> dict[str, Any]:  # pragma: no cover - debugging aid
    return {"tenant_id": str(row.tenant_id), "enabled": row.enabled}
