# ruff: file-ignore[line-too-long]
"""Anonymous Job Network site API (Epic 75, spec A6; prefix ``/api/v1/public/network``). No session, no CSRF. Reads are
rate limited (120/min per IP) and cached (``public, max-age=60, s-maxage=300``). Responses never carry ``tenant_id`` /
``member_id`` / ``posting_ref`` (except inside the already-public ``apply_url``) / hold reasons / report counts / stats."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from app.core import bot_challenge, rate_limit
from app.core.context import get_context
from app.schemas.job_network import (
    ImpressionBeaconIn,
    NetworkAgency,
    NetworkJob,
    NetworkJobPage,
    NetworkSitemapItem,
    ReportIn,
)
from app.services.job_network import reports, search, stats
from app.services.job_network.reports import ip_hash

router = APIRouter(prefix="/public/network", tags=["job-network-public"])
CACHE = {"Cache-Control": "public, max-age=60, s-maxage=300"}
READS_PER_MINUTE = 120


async def _read_guard(response: Response) -> None:
    await rate_limit.enforce("network_read", ip_hash(get_context().ip), READS_PER_MINUTE, 60)
    for key, value in CACHE.items():
        response.headers[key] = value


ReadGuard = Annotated[None, Depends(_read_guard)]


@router.get("/jobs", response_model=NetworkJobPage)
async def list_jobs(
    _: ReadGuard,
    q: Annotated[str | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    skills: Annotated[list[str] | None, Query()] = None,
    work_mode: Annotated[str | None, Query()] = None,
    employment_type: Annotated[str | None, Query()] = None,
    experience: Annotated[float | None, Query()] = None,
    pay_min: Annotated[float | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    posted_within: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    facets: Annotated[int, Query()] = 0,
) -> NetworkJobPage:
    return await search.query(
        q=q, location=location, skills=skills, work_mode=work_mode, employment_type=employment_type,
        experience=experience, pay_min=pay_min, category=category, posted_within=posted_within, page=page,
        page_size=page_size, facets=bool(facets),
    )


@router.get("/jobs/{listing_id}", response_model=NetworkJob)
async def get_job(listing_id: uuid.UUID, _: ReadGuard) -> NetworkJob:
    job = await search.get_job(listing_id)
    await stats.record_view(listing_id)
    return job


@router.get("/jobs/{listing_id}/apply")
async def apply(listing_id: uuid.UUID, _: ReadGuard) -> RedirectResponse:
    listing = await search.listed_listing(listing_id)
    if listing is None:
        from app.core.errors import NotFound

        raise NotFound("Job not found", code="listing_not_found")
    await stats.record_apply_click(listing_id)
    return RedirectResponse(listing.apply_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs/{listing_id}/report", status_code=status.HTTP_202_ACCEPTED)
async def report(listing_id: uuid.UUID, body: ReportIn, response: Response) -> dict[str, str]:
    ip = get_context().ip
    await rate_limit.enforce("network_report", ip_hash(ip), 5, 86400)
    await bot_challenge.verify_or_fail(body.challenge_token, ip)
    await reports.create(listing_id, reason=body.reason, details=body.details, ip=ip)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "received"}


@router.get("/agencies/{public_slug}", response_model=NetworkAgency)
async def agency(public_slug: str, _: ReadGuard) -> NetworkAgency:
    return await search.agency(public_slug)


@router.get("/sitemap", response_model=list[NetworkSitemapItem])
async def sitemap(_: ReadGuard, page: Annotated[int, Query(ge=1)] = 1) -> list[NetworkSitemapItem]:
    return await search.sitemap(page)


@router.post("/stats/impressions", status_code=status.HTTP_204_NO_CONTENT)
async def impressions(body: ImpressionBeaconIn) -> Response:
    await rate_limit.enforce("network_read", ip_hash(get_context().ip), READS_PER_MINUTE, 60)
    await stats.record_impressions(list(body.listing_ids))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
