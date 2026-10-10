# ruff: noqa: E501
"""The anonymous XML job feed (Epic 70, spec A6): ``GET /api/v1/public/job-feed/{tenant_slug}/{feed_token}.xml``.
404 unless the tenant site is active, the feed enabled and the token matches; ``IP_ANON``-style limit 60/min;
``Cache-Control: public, max-age=900``; ETag / ``If-None-Match``."""

from typing import Annotated

from fastapi import APIRouter, Header, Path, Response

from app.core import rate_limit
from app.core.context import get_context
from app.services import job_distribution_service as svc
from app.services.portal.common import ip_hash

router = APIRouter(prefix="/public/job-feed", tags=["job-feed"])


@router.get("/{slug}/{token}.xml", response_class=Response)
async def job_feed(
    slug: Annotated[str, Path(min_length=1, max_length=100)],
    token: Annotated[str, Path(min_length=1, max_length=64)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=200)] = None,
) -> Response:
    await rate_limit.enforce("job_feed_read", ip_hash(get_context().ip), 60, 60)
    body, etag = await svc.render_feed(slug, token)
    headers = {"Cache-Control": "public, max-age=900", "ETag": etag}
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/xml; charset=utf-8", headers=headers)
