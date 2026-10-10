"""Public Career Portal API (Epic 9 stories 9.1 / 9.2): ``/api/v1/public/careers/{slug}`` + candidate sign-in
(``/api/v1/public/candidate-auth``) + the ``data_export_download`` secure-link download.

No session and no CSRF. Reads: 120 / min per IP, ``Cache-Control: public, max-age=60, s-maxage=60``. POSTs
additionally require an allow-listed ``Origin`` (``FRONTEND_URL`` or ``https://<slug>.<CAREERS_ROOT_DOMAIN>``).
"""

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Header,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core import rate_limit
from app.core.context import get_context
from app.core.errors import Forbidden
from app.router.public import NO_STORE, LinkToken
from app.schemas.careers import (
    ApplySessionCreate,
    ApplySubmit,
    CandidateLinkRequest,
    CandidateLinkVerify,
    PublicApplyForm,
    PublicApplySession,
    PublicBranding,
    PublicPosting,
    PublicPostingPage,
    PublicResumeResult,
    PublicSitemapEntry,
    PublicStatus,
)
from app.schemas.portal import PortalMe
from app.services import custom_domains, files_service
from app.services import portal as portal_service
from app.services.portal.common import ip_hash

router = APIRouter(prefix="/public/careers/{slug}", tags=["careers"])
auth_router = APIRouter(prefix="/public/candidate-auth", tags=["candidate-auth"])
links_router = APIRouter(prefix="/public/links", tags=["public"])

Slug = Annotated[str, Path(min_length=1, max_length=100)]
CACHE = {"Cache-Control": "public, max-age=60, s-maxage=60"}
READS_PER_MINUTE = 120


async def _read_guard(response: Response) -> None:
    await rate_limit.enforce("careers_read", ip_hash(get_context().ip), READS_PER_MINUTE, 60)
    for key, value in CACHE.items():
        response.headers[key] = value


async def _write_guard(request: Request, slug: Slug, response: Response) -> None:
    origin = (request.headers.get("origin") or "").rstrip("/").lower()
    if origin not in {o.lower() for o in portal_service.careers.allowed_origins(slug)} and not (
        origin.startswith("https://") and await custom_domains.slug_for_host(origin) == slug
    ):  # Epic 33: the tenant's verified custom domain is an allowed origin too
        raise Forbidden("Cross-origin request refused", code="origin_not_allowed")
    response.headers["Cache-Control"] = "no-store"


async def _site_slug(request: Request, slug: Slug) -> str:
    """Epic 33 (FR-174, AD-38): a verified custom domain in ``X-Forwarded-Host`` / ``Host`` selects its tenant's
    site; otherwise the path slug (subdomain / path mode) is used."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return await custom_domains.slug_for_host(host) or slug


SiteSlug = Annotated[str, Depends(_site_slug)]
ReadGuard = Annotated[None, Depends(_read_guard)]
WriteGuard = Annotated[None, Depends(_write_guard)]
ApplyToken = Annotated[str | None, Header(alias="X-Apply-Token", max_length=200)]


# ================================================================ reads
@router.get("", response_model=PublicBranding)
async def branding(slug: SiteSlug, _: ReadGuard) -> PublicBranding:
    return await portal_service.careers.get_branding(slug)


@router.get("/postings", response_model=PublicPostingPage)
async def list_postings(
    slug: SiteSlug,
    _: ReadGuard,
    q: Annotated[str | None, Query(max_length=200)] = None,
    location: Annotated[str | None, Query(max_length=100)] = None,
    work_mode: Annotated[str | None, Query(max_length=20)] = None,
    employment_type: Annotated[str | None, Query(max_length=30)] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PublicPostingPage:
    return await portal_service.careers.list_postings(
        slug,
        q=q,
        location=location,
        work_mode=work_mode,
        employment_type=employment_type,
        page=page,
        page_size=page_size,
    )


@router.get("/postings/{posting_id}", response_model=PublicPosting)
async def get_posting(slug: SiteSlug, posting_id: uuid.UUID, _: ReadGuard) -> PublicPosting:
    return await portal_service.careers.get_posting(slug, posting_id)


@router.get("/sitemap", response_model=list[PublicSitemapEntry])
async def sitemap(slug: SiteSlug, _: ReadGuard) -> list[PublicSitemapEntry]:
    return await portal_service.careers.sitemap(slug)


@router.get("/postings/{posting_id}/apply-form", response_model=PublicApplyForm)
async def apply_form(slug: SiteSlug, posting_id: uuid.UUID, _: ReadGuard) -> PublicApplyForm:
    return await portal_service.careers.apply_form(slug, posting_id)


@router.get("/logo", response_class=Response, responses={200: {"content": {"image/png": {}}}})
async def logo(slug: SiteSlug, _: ReadGuard) -> Response:
    content, content_type = await portal_service.careers.get_asset(slug, "logo")
    return Response(
        content=content, media_type=content_type, headers={**CACHE, "X-Content-Type-Options": "nosniff"}
    )


@router.get("/header", response_class=Response, responses={200: {"content": {"image/png": {}}}})
async def header(slug: SiteSlug, _: ReadGuard) -> Response:
    content, content_type = await portal_service.careers.get_asset(slug, "header")
    return Response(
        content=content, media_type=content_type, headers={**CACHE, "X-Content-Type-Options": "nosniff"}
    )


# ================================================================ apply
@router.post(
    "/postings/{posting_id}/apply-sessions",
    response_model=PublicApplySession,
    status_code=status.HTTP_201_CREATED,
)
async def create_apply_session(
    slug: SiteSlug, posting_id: uuid.UUID, body: ApplySessionCreate, _: WriteGuard
) -> PublicApplySession:
    ctx = get_context()
    return await portal_service.apply.create_session(
        slug, posting_id, body, ip=ctx.ip, user_agent=ctx.user_agent
    )


@router.post("/apply-sessions/resume", response_model=PublicResumeResult)
async def upload_resume(
    slug: SiteSlug, file: Annotated[UploadFile, File()], _: WriteGuard, token: ApplyToken = None
) -> PublicResumeResult:
    data = await files_service.read_upload(file)
    return await portal_service.apply.upload_resume(slug, token, data, file.filename)


@router.post("/apply-sessions/submit", response_model=PublicStatus, status_code=status.HTTP_202_ACCEPTED)
async def submit_application(
    slug: SiteSlug, body: ApplySubmit, _: WriteGuard, token: ApplyToken = None
) -> PublicStatus:
    ctx = get_context()
    return await portal_service.apply.submit(slug, token, body, ip=ctx.ip, user_agent=ctx.user_agent)


# ================================================================ candidate sign-in
@auth_router.post("/request", response_model=PublicStatus, status_code=status.HTTP_202_ACCEPTED)
async def request_link(
    body: CandidateLinkRequest, background: BackgroundTasks, response: Response
) -> PublicStatus:
    response.headers["Cache-Control"] = "no-store"
    return await portal_service.candidate_auth.request_link(body, background, ip=get_context().ip)


@auth_router.post("/verify", response_model=PortalMe)
async def verify_link(body: CandidateLinkVerify, response: Response) -> PortalMe:
    for key, value in NO_STORE.items():
        response.headers[key] = value
    ctx = get_context()
    return await portal_service.candidate_auth.verify(
        body.token, response, ip=ctx.ip, user_agent=ctx.user_agent
    )


# ================================================================ export download (email secure link)
@links_router.post(
    "/{token}/download", response_class=Response, responses={200: {"content": {"application/zip": {}}}}
)
async def download_export(token: LinkToken, request: Request) -> Response:
    from app.core.settings import get_settings

    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin != get_settings().frontend_url.rstrip("/"):
        raise Forbidden("Cross-origin request refused", code="origin_not_allowed")
    content, filename = await portal_service.privacy_self_service.public_download(token)
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "export.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{safe}"'},
    )
