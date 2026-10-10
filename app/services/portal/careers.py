"""Public Career Portal (Epic 9 story 9.1, FR-46 / FR-47): branding, posting search / detail with JSON-LD,
sitemap, apply form, and the posting search vector.

* ``resolve_site(slug)``: the platform ``tenants`` row (citext slug) must be ``active`` and its
  ``career_sites`` row ``enabled``; otherwise 404 ``career_site_not_found``. Every read then runs in
  ``tenant_session(tenant_id)`` (RLS), so another tenant's posting is simply not found.
* Only live postings of Open demands are listed, shown, applied to or put in the sitemap (else 404).
* Responses are built by ``projection`` only.
"""

import dataclasses
import uuid
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import bot_challenge
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound
from app.core.settings import get_settings
from app.models.clients import Client
from app.models.demands import Demand, DemandPosting, DemandSkill, JdVersion
from app.models.identity import Tenant
from app.models.portal import CareerSite
from app.schemas.careers import (
    PublicApplyForm,
    PublicBranding,
    PublicChallenge,
    PublicPosting,
    PublicPostingPage,
    PublicSitemapEntry,
)
from app.services import files_service
from app.services.portal import projection

LIVE_DEMAND_STATUSES = ("open",)
NOTICE_PURPOSES = ("data_processing", "ai_processing", "marketing")
ACCEPTED_TYPES = ["pdf", "docx"]
MAX_PAGE_SIZE = 50
SEARCH_CONFIG = "english"

# One statement (also used by migration 20261001_90 for the backfill): slug from the title (kebab-case,
# <= 80 chars) and the weighted search vector (title A, skills B, JD text C).
REFRESH_SQL = """
UPDATE demand_postings AS p SET
    slug = COALESCE(NULLIF(left(trim(both '-' from regexp_replace(lower(d.title), '[^a-z0-9]+', '-', 'g')), 80), ''), 'job'),
    search_tsv =
        setweight(to_tsvector('english', coalesce(d.title, '')), 'A')
        || setweight(to_tsvector('english', coalesce((
            SELECT string_agg(s.skill_label, ' ') FROM demand_skills s WHERE s.demand_id = d.id
        ), '')), 'B')
        || setweight(to_tsvector('english', coalesce(v.content, '')), 'C')
FROM demands AS d, jd_versions AS v
WHERE p.demand_id = d.id AND v.id = p.jd_version_id
"""


def site_not_found() -> NotFound:
    return NotFound("Career site not found", code="career_site_not_found")


def posting_not_found() -> NotFound:
    return NotFound("Posting not found", code="posting_not_found")


@dataclasses.dataclass(frozen=True, slots=True)
class SiteRef:
    tenant_id: uuid.UUID
    slug: str
    tenant_name: str
    display_name: str
    logo_file_id: uuid.UUID | None
    header_file_id: uuid.UUID | None
    brand_color: str | None
    about_html: str | None
    privacy_contact_email: str | None
    stage_labels: dict[str, Any]
    apply_requires_phone: bool


async def tenant_by_slug(slug: str) -> Tenant | None:
    if not slug or len(slug) > 100:
        return None
    async with platform_session() as pdb:
        tenant = (await pdb.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    return tenant if tenant is not None and tenant.status == "active" else None


def site_ref(tenant: Tenant, site: CareerSite | None) -> SiteRef:
    return SiteRef(
        tenant_id=tenant.id,
        slug=str(tenant.slug),
        tenant_name=tenant.name,
        display_name=(site.display_name if site and site.display_name else tenant.name),
        logo_file_id=site.logo_file_id if site else None,
        header_file_id=site.header_file_id if site else None,
        brand_color=site.brand_color if site else None,
        about_html=site.about_html if site else None,
        privacy_contact_email=str(site.privacy_contact_email)
        if site and site.privacy_contact_email
        else None,
        stage_labels=dict(site.candidate_stage_labels or {}) if site else {},
        apply_requires_phone=bool(site.apply_requires_phone) if site else False,
    )


async def resolve_site(slug: str) -> SiteRef:
    tenant = await tenant_by_slug(slug)
    if tenant is None:
        raise site_not_found()
    async with tenant_session(tenant.id) as db:
        site = await db.get(CareerSite, tenant.id)
    if site is None or not site.enabled:
        raise site_not_found()
    return site_ref(tenant, site)


async def site_for_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> SiteRef | None:
    """Branding of a tenant for portal pages / emails (enabled or not)."""
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
    if tenant is None:
        return None
    return site_ref(tenant, await db.get(CareerSite, tenant_id))


# ================================================================ postings (read model)
def _live() -> ColumnElement[bool]:
    # Epic 36 (AD-50): ``visibility = 'internal'`` demands are hidden from list, detail, sitemap and apply.
    return and_(
        DemandPosting.is_live.is_(True),
        Demand.status.in_(LIVE_DEMAND_STATUSES),
        Demand.archived_at.is_(None),
        Demand.visibility != "internal",
    )


def _row(p: DemandPosting, d: Demand, client_name: Any) -> dict[str, Any]:
    return {
        "posting_id": p.id,
        "demand_id": d.id,
        "slug": p.slug,
        "title": d.title,
        "show_rate": p.show_rate,
        "show_location": p.show_location,
        "show_client_name": p.show_client_name,
        "published_at": p.published_at,
        "lastmod": max(filter(None, (p.published_at, p.updated_at)), default=None),
        "location": d.location,
        "country": d.country,
        "work_mode": d.work_mode,
        "employment_type": d.employment_type,
        "min_experience_years": d.min_experience_years,
        "max_experience_years": d.max_experience_years,
        "pay_rate_min": d.pay_rate_min,
        "pay_rate_max": d.pay_rate_max,
        "currency": d.currency,
        "rate_unit": d.rate_unit,
        "client_name": client_name,
        "apply_questions": list(p.apply_questions or []),
        "jd_version_id": p.jd_version_id,
        "owner_id": d.owner_id,
    }


def _base() -> Select[tuple[DemandPosting, Demand, str]]:
    return (
        select(DemandPosting, Demand, Client.name)
        .join(Demand, Demand.id == DemandPosting.demand_id)
        .join(Client, Client.id == Demand.client_id)
        .where(_live())
    )


async def live_posting(db: AsyncSession, posting_id: uuid.UUID) -> dict[str, Any]:
    """Return the live posting (dict row) or 404 -- unpublished, on hold, unknown and other tenants alike."""
    row = (await db.execute(_base().where(DemandPosting.id == posting_id))).first()
    if row is None:
        raise posting_not_found()
    return _row(*row)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_postings(
    db: AsyncSession,
    *,
    q: str | None,
    location: str | None,
    work_mode: str | None,
    employment_type: str | None,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    stmt = _base()
    if q and q.strip():
        stmt = stmt.where(
            DemandPosting.search_tsv.op("@@")(func.websearch_to_tsquery(SEARCH_CONFIG, q.strip()[:200]))
        )
    if location and location.strip():
        pattern = f"%{_escape_like(location.strip()[:100])}%"
        # Hidden locations are never matched (a filter must not reveal them).
        stmt = stmt.where(DemandPosting.show_location.is_(True), Demand.location.ilike(pattern, escape="\\"))
    if work_mode:
        stmt = stmt.where(Demand.work_mode == work_mode)
    if employment_type:
        stmt = stmt.where(Demand.employment_type == employment_type)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(DemandPosting.published_at.desc().nulls_last(), DemandPosting.id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    return [_row(*r) for r in rows], total


async def _detail_extras(db: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    version = await db.get(JdVersion, row["jd_version_id"])
    skills = (
        await db.execute(
            select(DemandSkill.skill_label)
            .where(DemandSkill.demand_id == row["demand_id"])
            .order_by(DemandSkill.position, DemandSkill.skill_label)
        )
    ).scalars()
    return {**row, "content": version.content if version is not None else "", "skills": list(skills)}


async def refresh_posting_search(db: AsyncSession, demand_id: uuid.UUID) -> None:
    """Recompute the slug and search vector of the demand's posting (on ``DemandPublished``)."""
    await db.execute(text(REFRESH_SQL + " AND p.demand_id = :demand_id"), {"demand_id": demand_id})


async def refresh_all(db: AsyncSession) -> None:
    await db.execute(text(REFRESH_SQL))


# ================================================================ public API
async def get_branding(slug: str) -> PublicBranding:
    return projection.branding(await resolve_site(slug))


async def list_postings(
    slug: str,
    *,
    q: str | None = None,
    location: str | None = None,
    work_mode: str | None = None,
    employment_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PublicPostingPage:
    site = await resolve_site(slug)
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)
    async with tenant_session(site.tenant_id) as db:
        rows, total = await search_postings(
            db,
            q=q,
            location=location,
            work_mode=work_mode,
            employment_type=employment_type,
            page=page,
            page_size=page_size,
        )
    return PublicPostingPage(
        items=[projection.posting_summary(r) for r in rows], total=total, page=page, page_size=page_size
    )


async def get_posting(slug: str, posting_id: uuid.UUID) -> PublicPosting:
    site = await resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        row = await _detail_extras(db, await live_posting(db, posting_id))
    return projection.posting_detail(row, site)


async def sitemap(slug: str) -> list[PublicSitemapEntry]:
    site = await resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        rows = (
            await db.execute(_base().order_by(DemandPosting.published_at.desc().nulls_last()).limit(5000))
        ).all()
    return [projection.sitemap_entry(_row(*r)) for r in rows]


async def apply_form(slug: str, posting_id: uuid.UUID) -> PublicApplyForm:
    from app.services import privacy

    site = await resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        row = await live_posting(db, posting_id)
        notices = [await privacy.consent.active_notice(db, site.tenant_id, p) for p in NOTICE_PURPOSES]
    return PublicApplyForm(
        posting_id=row["posting_id"],
        title=row["title"],
        notices=[projection.notice(n) for n in notices if n is not None],
        apply_questions=[projection.question(q) for q in row["apply_questions"]],
        max_upload_mb=get_settings().max_upload_mb,
        accepted_types=list(ACCEPTED_TYPES),
        challenge=PublicChallenge(**bot_challenge.public_config()),
        requires_phone=site.apply_requires_phone,
    )


async def get_asset(slug: str, which: str) -> tuple[bytes, str]:
    """Public logo / header image of an enabled career site."""
    site = await resolve_site(slug)
    file_id = site.logo_file_id if which == "logo" else site.header_file_id if which == "header" else None
    if file_id is None:
        raise NotFound("Image not found")
    async with tenant_session(site.tenant_id) as db:
        file = await files_service.get_file(db, file_id)
        if (
            file is None
            or file.deleted_at is not None
            or file.scan_status not in files_service.DOWNLOADABLE_SCAN_STATUSES
        ):
            raise NotFound("Image not found")
        return await files_service.read_file(file), file.content_type


def allowed_origins(slug: str) -> set[str]:
    s = get_settings()
    origins = {s.frontend_url.rstrip("/")}
    if s.careers_root_domain:
        origins.add(f"https://{slug.lower()}.{s.careers_root_domain.strip('.').lower()}")
    return origins
