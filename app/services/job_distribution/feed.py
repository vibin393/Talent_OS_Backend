# ruff: file-ignore[line-too-long]
"""The per-tenant XML job feed (Epic 70, spec A6; Indeed XML feed conventions).

``GET /api/v1/public/job-feed/{tenant_slug}/{feed_token}.xml``: 404 unless the tenant is active, its careers site is
enabled, ``job_feed_settings.enabled`` and the token matches (constant time). Lists every ``job_feed`` Board Posting
that is live whose careers posting is live, with only the fields the posting's flags allow (salary only when the
posting shows the rate AND ``include_salary``; the client name only when the posting shows it and the mode is
``client``). No candidate data can appear (the feed is built from postings only).
"""

import hashlib
import hmac
import secrets
import uuid
from email.utils import format_datetime
from xml.sax.saxutils import escape  # nosec B406 - xml.sax.saxutils escape/quoteattr are output encoders, not parsers

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.job_distribution import BoardPosting, JobFeedSettings
from app.schemas.job_distribution import FeedSettingsIn, FeedSettingsOut
from app.services.job_distribution import mapping
from app.services.job_distribution.common import now, tenant_of

JOBTYPE = {"permanent": "fulltime", "contract": "contract", "contract_to_hire": "contract", "freelance": "temporary"}
REMOTE = {"remote": "remote", "hybrid": "hybrid"}


async def settings_row(db: AsyncSession) -> JobFeedSettings:
    row = (await db.execute(select(JobFeedSettings))).scalar_one_or_none()
    if row is None:
        row = JobFeedSettings(id=uuid.uuid4(), tenant_id=tenant_of(db), enabled=False, feed_token=secrets.token_urlsafe(32), include_salary=False, company_name_mode="tenant")
        db.add(row)
        await db.flush()
    return row


def feed_url(slug: str, token: str) -> str:
    from app.core.settings import get_settings

    return f"{get_settings().p5_public_webhook_base_url()}/api/v1/public/job-feed/{slug}/{token}.xml"


async def settings_out(db: AsyncSession) -> FeedSettingsOut:
    row = await settings_row(db)
    slug, _ = await mapping.tenant_slug_and_name(db)
    return FeedSettingsOut(enabled=row.enabled, include_salary=row.include_salary, company_name_mode=row.company_name_mode, feed_url=feed_url(slug, row.feed_token))


async def update_settings(db: AsyncSession, data: FeedSettingsIn) -> FeedSettingsOut:
    row = await settings_row(db)
    for field in ("enabled", "include_salary", "company_name_mode"):
        value = getattr(data, field)
        if value is not None:
            setattr(row, field, value)
    await db.flush()
    return await settings_out(db)


async def regenerate(db: AsyncSession) -> FeedSettingsOut:
    row = await settings_row(db)
    row.feed_token = secrets.token_urlsafe(32)
    await db.flush()
    return await settings_out(db)


def _cdata(text: str) -> str:
    return "<![CDATA[" + (text or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


async def render(slug: str, token: str) -> tuple[bytes, str]:
    """``(xml, etag)``; 404 for every refusal (no enumeration)."""
    from app.services.portal import careers

    try:
        site = await careers.resolve_site(slug)
    except NotFound:
        raise NotFound("Not found") from None
    async with tenant_session(site.tenant_id) as db:
        row = (await db.execute(select(JobFeedSettings))).scalar_one_or_none()
        if row is None or not row.enabled or not token or not hmac.compare_digest(row.feed_token.encode(), token.encode()):
            raise NotFound("Not found")
        bps = list((await db.execute(select(BoardPosting).where(BoardPosting.board_key == "job_feed", BoardPosting.status.in_(("live", "expiring"))).order_by(BoardPosting.published_at.desc().nulls_last(), BoardPosting.id))).scalars())
        jobs: list[str] = []
        for bp in bps:
            src = await mapping.load_source(db, bp.posting_id)
            if src is None or not mapping.is_live(src):
                continue
            jobs.append(_job(src, bp, row))
    careers_url = mapping.careers_posting_url(site.slug, uuid.UUID(int=0)).rsplit("/jobs/", 1)[0]
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n<source>'
        f"<publisher>{escape(site.display_name)}</publisher><publisherurl>{escape(careers_url)}</publisherurl>"
        f"<lastBuildDate>{format_datetime(now())}</lastBuildDate>" + "".join(jobs) + "</source>\n"
    ).encode()
    etag = '"' + hashlib.sha256(b"".join(j.encode() for j in jobs)).hexdigest()[:32] + '"'
    return body, etag


def _job(src: mapping.Source, bp: BoardPosting, settings: JobFeedSettings) -> str:
    d, p = src.demand, src.posting
    dto = mapping.to_dto(src, "job_feed", bp.id, bp.local_revision, {})
    company = src.client_name if (p.show_client_name and settings.company_name_mode == "client" and src.client_name) else src.company
    parts = [
        f"<title>{_cdata(d.title)}</title>",
        f"<date>{format_datetime(bp.published_at or now())}</date>",
        f"<referencenumber>{escape(bp.human_id)}</referencenumber>",
        f"<requisitionid>{escape(d.req_id)}</requisitionid>",
        f"<url>{escape(mapping.careers_posting_url(src.slug, p.id) + f'?src=job_feed&bp={bp.id}')}</url>",
        f"<company>{_cdata(company or '')}</company>",
    ]
    if dto.location is not None:
        parts.append(f"<city>{escape(dto.location.city or '')}</city><state>{escape(dto.location.region or '')}</state><country>{escape(dto.location.country)}</country>")
    parts.append(f"<description>{_cdata(dto.description_html)}</description>")
    if dto.salary is not None and settings.include_salary:
        s = dto.salary
        parts.append(f"<salary>{escape(f'{s.currency} {s.min_amount or ""}-{s.max_amount or ""} per {s.period}')}</salary>")
    parts.append(f"<jobtype>{JOBTYPE.get(dto.employment_type, 'fulltime')}</jobtype>")
    if dto.experience_min_years is not None or dto.experience_max_years is not None:
        parts.append(f"<experience>{int(dto.experience_min_years or 0)}-{int(dto.experience_max_years) if dto.experience_max_years is not None else ''} years</experience>")
    parts.append(f"<remotetype>{REMOTE.get(dto.work_mode, '')}</remotetype>")
    if dto.expires_at is not None:
        parts.append(f"<expirationdate>{dto.expires_at.date().isoformat()}</expirationdate>")
    return "<job>" + "".join(parts) + "</job>"
