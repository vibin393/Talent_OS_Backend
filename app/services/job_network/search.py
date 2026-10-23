# ruff: file-ignore[line-too-long]
"""Public network site reads (Epic 75, spec A6; FR-387). Anonymous: only ``status='listed'`` rows of ``active``
members, no tenant boosting, no paid placement. Responses never carry ``tenant_id`` / ``member_id`` / ``posting_ref``
(except embedded in the already-public ``apply_url``) / hold reasons / report counts / stats."""

import csv
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.errors import NotFound
from app.core.settings import get_settings
from app.models.job_network import NetworkListing, NetworkMember
from app.schemas.job_network import (
    NetworkAgency,
    NetworkJob,
    NetworkJobFacets,
    NetworkJobPage,
    NetworkJobSummary,
    NetworkSitemapItem,
)
from app.services.job_network.constants import SEARCH_CONFIG

_POSTED_WITHIN = {"24h": 1, "7d": 7, "30d": 30}
_PAGE_MAX = 50


# ---------------------------------------------------------------- Indian city aliases
def _load_city_aliases() -> dict[str, str]:
    path = Path(__file__).parent / "data" / "city_aliases.csv"
    aliases: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if len(row) >= 2 and row[0].strip() and not row[0].startswith("#"):
                    canonical = row[0].strip().lower()
                    for alias in row:
                        aliases[alias.strip().lower()] = canonical
    except OSError:
        pass
    return aliases


_CITY_ALIASES = _load_city_aliases()


def _canonical_city(value: str) -> set[str]:
    low = value.strip().lower()
    canonical = _CITY_ALIASES.get(low, low)
    return {a for a, c in _CITY_ALIASES.items() if c == canonical} | {canonical, low}


# ---------------------------------------------------------------- base queries
def _listed() -> Select[Any]:
    return (
        select(NetworkListing, NetworkMember)
        .join(NetworkMember, NetworkMember.id == NetworkListing.member_id)
        .where(NetworkListing.status == "listed", NetworkMember.status == "active")
    )


def _summary(listing: NetworkListing, member: NetworkMember) -> NetworkJobSummary:
    return NetworkJobSummary(
        id=listing.id, title=listing.title, agency=member.display_name, agency_slug=member.public_slug,
        client_name=listing.client_name, city=listing.city, region=listing.region, country=listing.country,
        location_hidden=listing.location_hidden, work_mode=listing.work_mode,
        employment_type=listing.employment_type, pay_min=listing.pay_min, pay_max=listing.pay_max,
        pay_currency=listing.pay_currency, pay_period=listing.pay_period, published_at=listing.published_at,
    )


async def query(
    *,
    q: str | None = None,
    location: str | None = None,
    skills: list[str] | None = None,
    work_mode: str | None = None,
    employment_type: str | None = None,
    experience: float | None = None,
    pay_min: float | None = None,
    category: str | None = None,
    posted_within: str | None = None,
    page: int = 1,
    page_size: int = 20,
    facets: bool = False,
) -> NetworkJobPage:
    page = max(1, page)
    page_size = min(max(1, page_size), _PAGE_MAX)
    stmt = _listed()
    rank = None
    if q and q.strip():
        tsq = func.websearch_to_tsquery(SEARCH_CONFIG, q.strip()[:200])
        stmt = stmt.where(NetworkListing.search_tsv.op("@@")(tsq))
        # rank = relevance * recency_decay(published_at); recency_decay = 1/(1 + age_days/30)
        age_days = func.extract("epoch", func.now() - NetworkListing.published_at) / 86400.0
        rank = func.ts_rank_cd(NetworkListing.search_tsv, tsq) * (1.0 / (1.0 + age_days / 30.0))
    if location and location.strip():
        if location.strip().lower() == "remote":
            stmt = stmt.where(NetworkListing.work_mode == "remote")
        else:
            cities = _canonical_city(location)
            stmt = stmt.where(
                (NetworkListing.work_mode == "remote")
                | func.lower(func.coalesce(NetworkListing.city, "")).in_(cities)
                | func.lower(func.coalesce(NetworkListing.region, "")).in_(cities)
            )
    if work_mode:
        stmt = stmt.where(NetworkListing.work_mode == work_mode)
    if employment_type:
        stmt = stmt.where(NetworkListing.employment_type == employment_type)
    if category:
        stmt = stmt.where(NetworkListing.category == category)
    if skills:
        stmt = stmt.where(NetworkListing.skills.op("&&")([s.lower() for s in skills]))
    if experience is not None:
        stmt = stmt.where(
            (NetworkListing.experience_min.is_(None)) | (NetworkListing.experience_min <= experience)
        )
    if pay_min is not None:
        stmt = stmt.where((NetworkListing.pay_max.is_(None)) | (NetworkListing.pay_max >= pay_min))
    if posted_within and posted_within in _POSTED_WITHIN:
        cutoff = datetime.now(UTC) - timedelta(days=_POSTED_WITHIN[posted_within])
        stmt = stmt.where(NetworkListing.published_at >= cutoff)

    async with platform_session() as pdb:
        total = int(
            (await pdb.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )
        ordered = stmt.order_by(rank.desc()) if rank is not None else stmt.order_by(
            NetworkListing.published_at.desc().nulls_last()
        )
        rows = list(
            (await pdb.execute(ordered.offset((page - 1) * page_size).limit(page_size))).all()
        )
        items = [_summary(listing, member) for listing, member in rows]
        facet_data = await _facets(pdb, stmt) if facets else None
    return NetworkJobPage(items=items, total=total, page=page, page_size=page_size, facets=facet_data)


async def _facets(pdb: AsyncSession, stmt: Select[Any]) -> NetworkJobFacets:
    sub = stmt.subquery()

    async def _count_by(col: str, limit: int | None = None) -> dict[str, int]:
        c = sub.c[col]
        s = select(c, func.count()).where(c.is_not(None)).group_by(c).order_by(func.count().desc())
        if limit:
            s = s.limit(limit)
        return {str(k): int(v) for k, v in (await pdb.execute(s)).all()}

    return NetworkJobFacets(
        work_mode=await _count_by("work_mode"),
        employment_type=await _count_by("employment_type"),
        category=await _count_by("category"),
        cities=await _count_by("city", 20),
    )


async def get_job(listing_id: uuid.UUID) -> NetworkJob:
    async with platform_session() as pdb:
        row = (await pdb.execute(_listed().where(NetworkListing.id == listing_id))).first()
    if row is None:
        raise NotFound("Job not found", code="listing_not_found")
    listing, member = row
    return _job(listing, member)


def _job(listing: NetworkListing, member: NetworkMember) -> NetworkJob:
    return NetworkJob(
        id=listing.id, title=listing.title, agency=member.display_name, agency_slug=member.public_slug,
        agency_careers_url=member.careers_base_url, description_html=listing.description_html,
        client_name=listing.client_name, city=listing.city, region=listing.region, country=listing.country,
        location_hidden=listing.location_hidden, work_mode=listing.work_mode,
        employment_type=listing.employment_type, experience_min=listing.experience_min,
        experience_max=listing.experience_max, skills=list(listing.skills or []), category=listing.category,
        pay_min=listing.pay_min, pay_max=listing.pay_max, pay_currency=listing.pay_currency,
        pay_period=listing.pay_period, apply_url=listing.apply_url, posting_url=listing.posting_url,
        published_at=listing.published_at, valid_through=listing.valid_through, jsonld=_jsonld(listing, member),
    )


def _jsonld(listing: NetworkListing, member: NetworkMember) -> dict[str, Any]:
    base = (get_settings().network_site_url or f"{get_settings().frontend_url.rstrip('/')}/jobs").rstrip("/")
    data: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": listing.title,
        "description": listing.description_html or listing.title,
        "identifier": {"@type": "PropertyValue", "name": member.display_name, "value": str(listing.id)},
        "hiringOrganization": {"@type": "Organization", "name": listing.client_name or member.display_name},
        "directApply": False,
        "url": f"{base}/{listing.id}",
        "datePosted": listing.published_at.date().isoformat() if listing.published_at else None,
    }
    if listing.valid_through:
        data["validThrough"] = listing.valid_through.isoformat()
    if listing.work_mode == "remote":
        data["jobLocationType"] = "TELECOMMUTE"
    elif listing.city or listing.country:
        addr: dict[str, Any] = {"@type": "PostalAddress"}
        if listing.city:
            addr["addressLocality"] = listing.city
        if listing.country:
            addr["addressCountry"] = listing.country
        data["jobLocation"] = {"@type": "Place", "address": addr}
    if (listing.pay_min or listing.pay_max) and listing.pay_currency:
        value: dict[str, Any] = {"@type": "QuantitativeValue", "unitText": (listing.pay_period or "year").upper()}
        if listing.pay_min is not None:
            value["minValue"] = float(listing.pay_min)
        if listing.pay_max is not None:
            value["maxValue"] = float(listing.pay_max)
        data["baseSalary"] = {"@type": "MonetaryAmount", "currency": listing.pay_currency, "value": value}
    return data


async def agency(public_slug: str) -> NetworkAgency:
    async with platform_session() as pdb:
        member = (
            await pdb.execute(
                select(NetworkMember).where(
                    func.lower(NetworkMember.public_slug) == public_slug.lower(), NetworkMember.status == "active"
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise NotFound("Agency not found", code="agency_not_found")
        rows = list(
            (
                await pdb.execute(
                    _listed()
                    .where(NetworkListing.member_id == member.id)
                    .order_by(NetworkListing.published_at.desc().nulls_last())
                    .limit(20)
                )
            ).all()
        )
        total = int(
            (
                await pdb.execute(
                    select(func.count())
                    .select_from(NetworkListing)
                    .where(NetworkListing.member_id == member.id, NetworkListing.status == "listed")
                )
            ).scalar_one()
        )
    return NetworkAgency(
        display_name=member.display_name, public_slug=member.public_slug, logo_url=member.logo_url,
        about_text=member.about_text, careers_url=member.careers_base_url, open_jobs=total,
        jobs=[_summary(listing, m) for listing, m in rows],
    )


async def sitemap(page: int = 1, page_size: int = 5000) -> list[NetworkSitemapItem]:
    page = max(1, page)
    page_size = min(max(1, page_size), 5000)
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(NetworkListing.id, NetworkListing.synced_at)
                    .where(NetworkListing.status == "listed")
                    .order_by(NetworkListing.published_at.desc().nulls_last())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
    return [NetworkSitemapItem(listing_id=r[0], lastmod=r[1]) for r in rows]


async def listed_listing(listing_id: uuid.UUID) -> NetworkListing | None:
    """Return the listed row (used by the 303 apply route and the view beacon), or ``None``."""
    async with platform_session() as pdb:
        return (
            await pdb.execute(_listed().where(NetworkListing.id == listing_id).with_only_columns(NetworkListing))
        ).scalar_one_or_none()


__all__ = ["agency", "get_job", "listed_listing", "query", "sitemap"]
